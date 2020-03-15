#!/usr/bin/env python
"""
Zhiang Chen
Feb 2020
Localize targets using particle filter
"""

import sys
import os
import rospy
import rospkg
from darknet_ros_msgs.msg import BoundingBoxes
from darknet_ros_msgs.msg import ObjectCount
from sensor_msgs.msg import Image
from sensor_msgs.msg import CameraInfo
from image_geometry import PinholeCameraModel
from geometry_msgs.msg import PoseStamped, TwistStamped
import message_filters
from cv_bridge import CvBridge, CvBridgeError
import numpy as np
import cv2
import copy
from numpy.linalg import inv
from numpy.linalg import det
import tf
import sensor_msgs.point_cloud2 as pcl2
from sensor_msgs.msg import PointCloud2
import std_msgs.msg


ROS_BAG = True

class TargetTracker(object):
    def __init__(self, particle_nm=1000, z_range=(1, 10)):
        self.nm = particle_nm
        self.target_points = [] # a list of Nx3 ndarrays
        self.z_min = z_range[0] # the range of particles along z axis in camera coord system
        self.z_max = z_range[1]

        if not ROS_BAG:
            rospy.loginfo("checking tf from camera to base_link ...")
            listener = tf.TransformListener()
            while not rospy.is_shutdown():
                try:
                    now = rospy.Time.now()
                    listener.waitForTransform("base_link", "r200", now, rospy.Duration(4.0))
                    (trans, rot) = listener.lookupTransform("base_link", "r200", now)
                except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                    continue
        else:
            trans_vec = (0.1, 0,  - 0.01)
            trans = tf.transformations.translation_matrix(trans_vec)
            quaternion = (-0.6743797, 0.6743797, - 0.2126311, 0.2126311)
            rot = tf.transformations.quaternion_matrix(quaternion)

        self.T_camera2base = np.matmul(trans, rot)

        self.pcl_pub = rospy.Publisher("/target_localizer/points", PointCloud2, queue_size=10)

        camera_info = rospy.wait_for_message("/r200/depth/camera_info", CameraInfo)

        self.pinhole_camera_model = PinholeCameraModel()
        self.pinhole_camera_model.fromCameraInfo(camera_info)

        self.image_W, self.image_H = self.pinhole_camera_model.fullResolution()

        self.sub_bbox = message_filters.Subscriber('/bbox_tracker/bounding_boxes', BoundingBoxes)
        self.sub_pose = message_filters.Subscriber('/mavros/local_position/pose', PoseStamped)
        # self.sub_vel = message_filters.Subscriber('/mavros/local_position/velocity_local', TwistStamped)

        self.ts = message_filters.ApproximateTimeSynchronizer([self.sub_bbox, self.sub_pose], queue_size=100, slop=0.1)
        # self.ts = message_filters.TimeSynchronizer([self.sub_bbox, self.sub_pose, self.sub_vel], 10)
        self.ts.registerCallback(self.callback)
        print("target_localizer initialized!")

    def callback(self, bbox_msg, pose_msg):
        pose = pose_msg.pose
        bboxes = bbox_msg.bounding_boxes


        for i, bbox in enumerate(bboxes):
            print(bbox)
            if not self.checkBBoxOnEdge(bbox):
                cone = self.reprojectBBoxesCone(bbox)
                ids = self.checkPointsInCone(cone, pose)
                if not ids:
                    self.generatePoints(cone, pose, self.nm)
                else:
                    self.updatePoints(ids, bbox)

        print(len(self.target_points))

        variances = self.computeTargetsVariance()
        self.publishTargetPoints()

    def checkBBoxOnEdge(self, bbox, p=20):
        x1 = bbox.xmin
        y1 = bbox.ymin
        x2 = bbox.xmax
        y2 = bbox.ymax
        if x1 < p:
            return True # it is on the edge
        if y1 < p:
            return True
        if (self.image_W - x2) < p:
            return True
        if (self.image_H - y2) < p:
            return True

        return False


    def reprojectBBoxesCone(self, bbox):
        """
        :param bbox:
        :return: cone = np.asarray((ray1, ray2, ray3, ray4))
        """
        x1 = bbox.xmin
        y1 = bbox.ymin
        x2 = bbox.xmax
        y2 = bbox.ymax
        #  ray1  ray2
        #  ray3  ray4
        #  see the camera coordinate system: https://github.com/ZhiangChen/target_mapping
        #  and also the api description: http://docs.ros.org/diamondback/api/image_geometry/html/c++/classimage__geometry_1_1PinholeCameraModel.html#ad52a4a71c6f6d375d69865e40a117ca3
        ray1 = self.pinhole_camera_model.projectPixelTo3dRay((x1, y1))
        ray2 = self.pinhole_camera_model.projectPixelTo3dRay((x2, y1))
        ray3 = self.pinhole_camera_model.projectPixelTo3dRay((x2, y2))
        ray4 = self.pinhole_camera_model.projectPixelTo3dRay((x1, y2))
        ray1 = np.asarray(ray1)/ray1[2]
        ray2 = np.asarray(ray2)/ray2[2]
        ray3 = np.asarray(ray3)/ray3[2]
        ray4 = np.asarray(ray4)/ray4[2]
        cone = np.asarray((ray1, ray2, ray3, ray4))
        #print(cone)
        return cone

    def checkPointsInCone(self, cone, pose):
        # False: no points; True: points
        if len(self.target_points) == 0:
            return False

        ray1, ray2, ray3, ray4 = cone
        #       norm1
        # norm4        norm2
        #       norm3
        # the order of cross product determines the normal vector direction
        norm1 = np.cross(ray1, ray2)
        norm2 = np.cross(ray2, ray3)
        norm3 = np.cross(ray3, ray4)
        norm4 = np.cross(ray4, ray1)
        H = np.asarray((norm1, norm2, norm3, norm4))

        ids = []
        for i,points in enumerate(self.target_points):
            # convert points to camera coordinate system
            T_base2world = self.getTransformFromPose(pose)
            T_camera2world = np.matmul(T_base2world, self.T_camera2base)
            T_world2camera = inv(T_camera2world)
            points_w = np.insert(points, 3, 1, axis=1).transpose()
            points_c = np.matmul(T_world2camera, points_w)
            points_c = points_c[:3, :]
            points_occupancy = np.all(np.matmul(H, points_c) >= 0, axis=0)
            if np.any(points_occupancy):
                ids.append(i)
        if len(ids) >= 1:
            return ids
        else:
            return False

    def generatePoints(self, cone, pose, nm=1000):
        # register new target
        # cone = np.asarray((ray1, ray2, ray3, ray4))
        # 1. generate points on unit surface, in camera coordinate system
        # 2. randomize these points by varying elevations, in camera coordinate system
        # 3. convert to world coordinate system
        a = np.random.rand(4, nm)  # 4 x nm
        scaling = np.diag(1.0 / np.sum(a, axis=0)) # nm x nm
        a_ = np.matmul(a, scaling) # 4 x nm
        points = np.matmul(cone.transpose(), a_) # 3 x nm
        z = np.random.rand(nm) * (self.z_max - self.z_min) + self.z_min
        z = np.diag(z) # nm x nm
        points_c = np.matmul(points, z) # 3 x nm
        points_c = np.insert(points_c, 3, 1, axis=0) # 4 x nm

        T_base2world = self.getTransformFromPose(pose)
        T_camera2world = np.matmul(T_base2world, self.T_camera2base)
        points_w = np.matmul(T_camera2world, points_c) # 4 x nm
        points_w = points_w[:3, :].transpose() # nm x 3
        self.target_points.append(points_w)



    def updatePoints(self, ids, bbox):
        # return information gain
        # if there are multiple targets in the bbox, then update them all individually
        pass


    def computeTargetsVariance(self):
        return None


    def deregisterTarget(self, id):
        pass


    def getTransformFromPose(self, pose):
        trans = tf.transformations.translation_matrix((pose.position.x, pose.position.y, pose.position.z))
        rot = tf.transformations.quaternion_matrix((pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w))
        T = np.matmul(trans, rot)
        return T

    def publishTargetPoints(self):
        # convert target_points to pointcloud messages
        all_points = []
        for points in self.target_points:
            all_points = all_points + points.tolist()
            
        header = std_msgs.msg.Header()
        header.stamp = rospy.Time.now()
        header.frame_id = 'map'
        # create pcl from points
        pc_msg = pcl2.create_cloud_xyz32(header, all_points)
        self.pcl_pub.publish(pc_msg)



if __name__ == '__main__':
    rospy.init_node('target_localizer', anonymous=False)
    target_tracker = TargetTracker()

    try:
        rospy.spin()
    except rospy.ROSInterruptException:
        rospy.loginfo("Node killed!")