#!/usr/bin/env python3

import signal
import sys
import time

import rclpy
from rclpy.node import Node

from aimdk_msgs.msg import McLocomotionVelocity, MessageHeader
from aimdk_msgs.srv import SetMcInputSource


class InputLocomotionVelocity(Node):
    def __init__(self):
        super().__init__('input_locomotion_velocity')

        self.publisher = self.create_publisher(
            McLocomotionVelocity, '/aima/mc/locomotion/velocity', 10)
        self.client = self.create_client(
            SetMcInputSource, '/aimdk_5Fmsgs/srv/SetMcInputSource')

        self.source = 'node'
        self.forward_velocity = 0.0
        self.lateral_velocity = 0.0
        self.angular_velocity = 0.0

        self.max_forward_speed = 1.0
        self.min_forward_speed = 0.2
        self.max_lateral_speed = 1.0
        self.min_lateral_speed = 0.2
        self.max_angular_speed = 1.0
        self.min_angular_speed = 0.1

        self.get_logger().info('Input locomotion velocity node started')

    def register_input_source(self):
        self.get_logger().info('Registering input source...')

        timeout_sec = 8.0
        start = self.get_clock().now().nanoseconds / 1e9

        while not self.client.wait_for_service(timeout_sec=2.0):
            now = self.get_clock().now().nanoseconds / 1e9
            if now - start > timeout_sec:
                self.get_logger().error('Waiting for service timed out')
                return False
            self.get_logger().info('Waiting for input source service...')

        req = SetMcInputSource.Request()
        req.action.value = 1001
        req.input_source.name = self.source
        req.input_source.priority = 40
        req.input_source.timeout = 1000

        for i in range(8):
            req.request.header.stamp = self.get_clock().now().to_msg()
            future = self.client.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=0.25)

            if future.done():
                break

            self.get_logger().info(f'Trying to register input source... [{i}]')

        if not future.done():
            self.get_logger().error('Service call failed or timed out')
            return False

        try:
            response = future.result()
            state = response.response.state.value
            self.get_logger().info(
                f'Input source set successfully: state={state}, '
                f'task_id={response.response.task_id}')
            return True
        except Exception as e:
            self.get_logger().error(f'Service call exception: {str(e)}')
            return False

    def validate_velocity(self, value, min_speed, max_speed, label):
        if abs(value) < 0.005:
            return 0.0
        if abs(value) > max_speed or abs(value) < min_speed:
            raise ValueError(
                f'{label} velocity must be 0 or +/-({min_speed} ~ {max_speed})')
        return value

    def set_velocity(self, forward, lateral, angular):
        self.forward_velocity = self.validate_velocity(
            forward, self.min_forward_speed, self.max_forward_speed, 'forward')
        self.lateral_velocity = self.validate_velocity(
            lateral, self.min_lateral_speed, self.max_lateral_speed, 'lateral')
        self.angular_velocity = self.validate_velocity(
            angular, self.min_angular_speed, self.max_angular_speed, 'angular')

    def clear_velocity(self):
        self.forward_velocity = 0.0
        self.lateral_velocity = 0.0
        self.angular_velocity = 0.0

    def publish_velocity(self):
        msg = McLocomotionVelocity()
        msg.header = MessageHeader()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.source = self.source
        msg.forward_velocity = self.forward_velocity
        msg.lateral_velocity = self.lateral_velocity
        msg.angular_velocity = self.angular_velocity

        self.publisher.publish(msg)

    def publish_stop(self, duration_sec=0.5):
        self.clear_velocity()
        start = self.get_clock().now()
        while (self.get_clock().now() - start).nanoseconds / 1e9 < duration_sec:
            self.publish_velocity()
            rclpy.spin_once(self, timeout_sec=0.02)
            time.sleep(0.001)

    def run_motion(self, duration_sec):
        if duration_sec <= 0.0:
            raise ValueError('duration must be greater than 0')

        self.get_logger().info(
            f'Running motion for {duration_sec:.2f} seconds: '
            f'forward={self.forward_velocity:.2f} m/s, '
            f'lateral={self.lateral_velocity:.2f} m/s, '
            f'angular={self.angular_velocity:.2f} rad/s')

        start = self.get_clock().now()
        while (self.get_clock().now() - start).nanoseconds / 1e9 < duration_sec:
            self.publish_velocity()
            rclpy.spin_once(self, timeout_sec=0.02)
            time.sleep(0.001)

        self.publish_stop()
        self.get_logger().info('Motion finished, robot stopped')


global_node = None


def signal_handler(sig, frame):
    global global_node
    if global_node is not None:
        global_node.publish_stop()
        global_node.get_logger().info(
            f'Received signal {sig}, stopping and shutting down')
    if rclpy.ok():
        rclpy.shutdown()
    sys.exit(0)


def read_float(prompt):
    return float(input(prompt).strip())


def main(args=None):
    global global_node
    rclpy.init(args=args)

    node = InputLocomotionVelocity()
    global_node = node

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        if not node.register_input_source():
            node.get_logger().error('Input source registration failed, exiting')
            return

        forward = read_float(
            'Please enter forward velocity 0 or +/-(0.2 ~ 1.0) m/s: ')
        lateral = read_float(
            'Please enter lateral velocity 0 or +/-(0.2 ~ 1.0) m/s: ')
        angular = read_float(
            'Please enter angular velocity 0 or +/-(0.1 ~ 1.0) rad/s: ')
        duration = read_float('Please enter duration in seconds: ')

        node.set_velocity(forward, lateral, angular)
        node.run_motion(duration)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        node.get_logger().error(f'Program exited with exception: {e}')
        node.publish_stop()
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
