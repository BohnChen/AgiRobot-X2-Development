#!/usr/bin/env python3

import math
import struct

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import PointCloud2, PointField


class NearestLidarObject(Node):
    def __init__(self):
        super().__init__('nearest_lidar_object')

        self.topic_name = '/aima/hal/sensor/lidar_chest_front/lidar_pointcloud'
        qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=5,
        )

        self.subscription = self.create_subscription(
            PointCloud2, self.topic_name, self.pointcloud_callback, qos)
        self.last_print = self.get_clock().now()

        self.get_logger().info(
            f'Subscribing LIDAR PointCloud2: {self.topic_name}')

    def pointcloud_callback(self, msg: PointCloud2):
        nearest = self.find_nearest_distance(msg)

        now = self.get_clock().now()
        if (now - self.last_print).nanoseconds * 1e-9 < 1.0:
            return
        self.last_print = now

        if nearest is None:
            self.get_logger().warn('No valid object distance detected')
            return

        stamp_sec = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        self.get_logger().info(
            f'Nearest object distance: {nearest:.3f} m, '
            f'frame_id={msg.header.frame_id}, stamp={stamp_sec:.6f}')

    def find_nearest_distance(self, msg: PointCloud2):
        fields = {field.name: field for field in msg.fields}
        if not all(name in fields for name in ('x', 'y', 'z')):
            self.get_logger().error('PointCloud2 missing x/y/z fields')
            return None

        x_field = fields['x']
        y_field = fields['y']
        z_field = fields['z']
        if not self.is_float32_field(x_field, y_field, z_field):
            self.get_logger().error('PointCloud2 x/y/z fields must be FLOAT32')
            return None

        endian = '>' if msg.is_bigendian else '<'
        point_count = msg.width * msg.height
        nearest_sq = None

        for index in range(point_count):
            base = index * msg.point_step
            if base + msg.point_step > len(msg.data):
                break

            x = struct.unpack_from(endian + 'f', msg.data, base + x_field.offset)[0]
            y = struct.unpack_from(endian + 'f', msg.data, base + y_field.offset)[0]
            z = struct.unpack_from(endian + 'f', msg.data, base + z_field.offset)[0]

            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                continue

            distance_sq = x * x + y * y + z * z
            if distance_sq <= 1e-8:
                continue

            if nearest_sq is None or distance_sq < nearest_sq:
                nearest_sq = distance_sq

        if nearest_sq is None:
            return None

        return math.sqrt(nearest_sq)

    def is_float32_field(self, *fields):
        return all(field.datatype == PointField.FLOAT32 for field in fields)


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = NearestLidarObject()
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
