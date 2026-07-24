#!/usr/bin/env python3

from collections import deque

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from sensor_msgs.msg import CameraInfo, CompressedImage


class RearCameraUndistort(Node):
    def __init__(self):
        super().__init__('rear_camera_undistort')

        self.declare_parameter(
            'image_topic',
            '/aima/hal/sensor/rgb_head_rear/rgb_image/compressed')
        self.declare_parameter(
            'camera_info_topic',
            '/aima/hal/sensor/rgb_head_rear/camera_info')
        self.declare_parameter(
            'output_topic',
            '/aima/hal/sensor/rgb_head_rear/rgb_image/undistorted/compressed')
        self.declare_parameter('alpha', 0.0)
        self.declare_parameter('jpeg_quality', 90)

        self.image_topic = self.get_parameter('image_topic').value
        self.camera_info_topic = self.get_parameter('camera_info_topic').value
        self.output_topic = self.get_parameter('output_topic').value
        self.alpha = float(self.get_parameter('alpha').value)
        self.jpeg_quality = int(self.get_parameter('jpeg_quality').value)

        sensor_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=5,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        camera_info_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.RELIABLE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )

        self.camera_matrix = None
        self.dist_coeffs = None
        self.distortion_model = ''
        self.map1 = None
        self.map2 = None
        self.map_size = None
        self.arrivals = deque()
        self.last_print = self.get_clock().now()

        self.image_sub = self.create_subscription(
            CompressedImage, self.image_topic, self.image_callback, sensor_qos)
        self.camera_info_sub = self.create_subscription(
            CameraInfo, self.camera_info_topic, self.camera_info_callback,
            camera_info_qos)
        self.publisher = self.create_publisher(
            CompressedImage, self.output_topic, 10)

        self.get_logger().info(f'Subscribing image: {self.image_topic}')
        self.get_logger().info(f'Subscribing camera info: {self.camera_info_topic}')
        self.get_logger().info(f'Publishing undistorted image: {self.output_topic}')

    def camera_info_callback(self, msg: CameraInfo):
        self.camera_matrix = np.array(msg.k, dtype=np.float64).reshape(3, 3)
        self.dist_coeffs = np.array(msg.d, dtype=np.float64).reshape(-1, 1)
        self.distortion_model = msg.distortion_model.lower()
        self.map1 = None
        self.map2 = None
        self.map_size = None

        self.get_logger().info(
            f'CameraInfo updated: model={msg.distortion_model}, '
            f'size={msg.width}x{msg.height}, d_len={len(msg.d)}')

    def image_callback(self, msg: CompressedImage):
        self.update_arrivals()

        if self.camera_matrix is None or self.dist_coeffs is None:
            if self.should_print():
                self.get_logger().warn('Waiting for CameraInfo before undistortion')
            return

        image = self.decode_compressed_image(msg)
        if image is None:
            return

        height, width = image.shape[:2]
        if self.map_size != (width, height):
            if not self.build_undistort_map(width, height):
                return

        undistorted = cv2.remap(
            image, self.map1, self.map2, interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT)

        output_msg = self.encode_compressed_image(undistorted, msg)
        if output_msg is None:
            return

        self.publisher.publish(output_msg)

        if self.should_print():
            self.get_logger().info(
                f'Published undistorted image: {width}x{height}, '
                f'fps={self.get_fps():.1f}, model={self.distortion_model}')

    def decode_compressed_image(self, msg: CompressedImage):
        encoded = np.frombuffer(msg.data, dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if image is None:
            self.get_logger().error('Failed to decode compressed image')
        return image

    def encode_compressed_image(self, image, source_msg: CompressedImage):
        quality = max(1, min(100, self.jpeg_quality))
        ok, encoded = cv2.imencode(
            '.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            self.get_logger().error('Failed to encode undistorted image')
            return None

        msg = CompressedImage()
        msg.header = source_msg.header
        msg.format = 'jpeg'
        msg.data = encoded.tobytes()
        return msg

    def build_undistort_map(self, width, height):
        if self.dist_coeffs.size == 0:
            self.get_logger().warn('CameraInfo has empty distortion coefficients')
            return False

        size = (width, height)
        try:
            if self.is_fisheye_model():
                distortion = self.dist_coeffs[:4].reshape(4, 1)
                new_camera_matrix = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
                    self.camera_matrix, distortion, size, np.eye(3),
                    balance=self.alpha)
                self.map1, self.map2 = cv2.fisheye.initUndistortRectifyMap(
                    self.camera_matrix, distortion, np.eye(3),
                    new_camera_matrix, size, cv2.CV_16SC2)
            else:
                new_camera_matrix, _ = cv2.getOptimalNewCameraMatrix(
                    self.camera_matrix, self.dist_coeffs, size, self.alpha,
                    size)
                self.map1, self.map2 = cv2.initUndistortRectifyMap(
                    self.camera_matrix, self.dist_coeffs, None,
                    new_camera_matrix, size, cv2.CV_16SC2)
        except cv2.error as e:
            self.get_logger().error(f'Failed to build undistort map: {e}')
            return False

        self.map_size = size
        self.get_logger().info(
            f'Undistort map built: size={width}x{height}, '
            f'model={self.distortion_model}, alpha={self.alpha:.2f}')
        return True

    def is_fisheye_model(self):
        return self.distortion_model in ('fisheye', 'equidistant')

    def update_arrivals(self):
        now = self.get_clock().now()
        self.arrivals.append(now)
        while self.arrivals and (now - self.arrivals[0]).nanoseconds * 1e-9 > 1.0:
            self.arrivals.popleft()

    def get_fps(self):
        return float(len(self.arrivals))

    def should_print(self):
        now = self.get_clock().now()
        if (now - self.last_print).nanoseconds * 1e-9 >= 1.0:
            self.last_print = now
            return True
        return False


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = RearCameraUndistort()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
