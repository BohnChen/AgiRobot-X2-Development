#!/usr/bin/env python3

import math
import signal
import sys
import time

import rclpy
from rclpy.node import Node

from aimdk_msgs.msg import (
    CommonState,
    HandCommand,
    HandCommandArray,
    HandType,
    McActionCommand,
    McControlArea,
    McLocomotionVelocity,
    McPresetMotion,
    MessageHeader,
    RequestHeader,
)
from aimdk_msgs.srv import SetMcAction, SetMcInputSource, SetMcPresetMotion


class HeartWalkTurn(Node):
    def __init__(self):
        super().__init__('heart_walk_turn')

        self.velocity_publisher = self.create_publisher(
            McLocomotionVelocity, '/aima/mc/locomotion/velocity', 10)
        self.hand_publisher = self.create_publisher(
            HandCommandArray, '/aima/hal/joint/hand/command', 10)
        self.action_client = self.create_client(
            SetMcAction, '/aimdk_5Fmsgs/srv/SetMcAction')
        self.input_source_client = self.create_client(
            SetMcInputSource, '/aimdk_5Fmsgs/srv/SetMcInputSource')
        self.preset_motion_client = self.create_client(
            SetMcPresetMotion, '/aimdk_5Fmsgs/srv/SetMcPresetMotion')

        self.source = 'node'
        self.forward_velocity = 0.25
        self.turn_velocity = 0.7
        self.action_sleep_sec = 2.0
        self.heart_to_turn_sleep_sec = 5.0
        self.input_source_timeout = 30000
        self.heart_motion_id = 1007
        self.heart_area_id = 3

        self.get_logger().info('Heart walk turn node started')

    def wait_for_service(self, client, service_name):
        timeout_sec = 8.0
        start = self.get_clock().now().nanoseconds / 1e9

        while not client.wait_for_service(timeout_sec=2.0):
            now = self.get_clock().now().nanoseconds / 1e9
            if now - start > timeout_sec:
                self.get_logger().error(f'Waiting for {service_name} timed out')
                return False
            self.get_logger().info(f'Waiting for {service_name}...')

        return True

    def register_input_source(self):
        if not self.wait_for_service(
                self.input_source_client,
                '/aimdk_5Fmsgs/srv/SetMcInputSource'):
            return False

        req = SetMcInputSource.Request()
        req.action.value = 100
        req.input_source.name = self.source
        req.input_source.priority = 40
        req.input_source.timeout = self.input_source_timeout

        for i in range(8):
            req.request.header.stamp = self.get_clock().now().to_msg()
            future = self.input_source_client.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=0.25)

            if future.done():
                break

            self.get_logger().info(f'Trying to register input source... [{i}]')

        if not future.done():
            self.get_logger().error('Input source service call timed out')
            return False

        response = future.result()
        if response is None:
            self.get_logger().error('Input source service call failed')
            return False

        self.get_logger().info(
            f'Input source set: state={response.response.state.value}, '
            f'task_id={response.response.task_id}')
        return True

    def set_mc_action(self, action_name):
        if not self.wait_for_service(
                self.action_client,
                '/aimdk_5Fmsgs/srv/SetMcAction'):
            return False

        req = SetMcAction.Request()
        req.header = RequestHeader()
        req.source = self.source
        req.command = McActionCommand()
        req.command.action_desc = action_name

        for i in range(8):
            req.header.stamp = self.get_clock().now().to_msg()
            future = self.action_client.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=0.25)

            if future.done():
                break

            self.get_logger().info(
                f'Trying to set MC action {action_name}... [{i}]')

        if not future.done():
            self.get_logger().error(f'MC action timed out: {action_name}')
            return False

        response = future.result()
        if response is None:
            self.get_logger().error(f'MC action failed: {action_name}')
            return False

        if response.response.status.value == CommonState.SUCCESS:
            self.get_logger().info(f'MC action set: {action_name}')
            return True

        self.get_logger().error(
            f'MC action rejected: {action_name}, '
            f'message={response.response.message}')
        return False

    def prepare_locomotion(self):
        if not self.set_mc_action('LOCOMOTION_DEFAULT'):
            return False
        time.sleep(1.0)
        return self.register_input_source()

    def prepare_stand(self):
        self.stop_motion()
        if not self.set_mc_action('STAND_DEFAULT'):
            return False
        time.sleep(1.0)
        return True

    def publish_velocity(self, forward, lateral, angular):
        msg = McLocomotionVelocity()
        msg.header = MessageHeader()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.source = self.source
        msg.forward_velocity = float(forward)
        msg.lateral_velocity = float(lateral)
        msg.angular_velocity = float(angular)

        self.velocity_publisher.publish(msg)

    def publish_velocity_for(self, forward, lateral, angular, duration_sec):
        start = self.get_clock().now()
        while (self.get_clock().now() - start).nanoseconds / 1e9 < duration_sec:
            self.publish_velocity(forward, lateral, angular)
            rclpy.spin_once(self, timeout_sec=0.02)
            time.sleep(0.001)

    def stop_motion(self, hold_sec=0.5):
        self.get_logger().info('Stopping locomotion')
        self.publish_velocity_for(0.0, 0.0, 0.0, hold_sec)

    def sleep_after_action(self, duration_sec=None):
        if duration_sec is None:
            duration_sec = self.action_sleep_sec

        self.get_logger().info(
            f'Sleeping for {duration_sec:.1f} seconds after action')
        time.sleep(duration_sec)

    def send_preset_motion(self, area_id, motion_id):
        if not self.wait_for_service(
                self.preset_motion_client,
                '/aimdk_5Fmsgs/srv/SetMcPresetMotion'):
            return False

        req = SetMcPresetMotion.Request()
        req.header = RequestHeader()
        req.motion = McPresetMotion(value=motion_id)
        req.area = McControlArea(value=area_id)
        req.interrupt = False

        for i in range(8):
            req.header.stamp = self.get_clock().now().to_msg()
            future = self.preset_motion_client.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=0.25)

            if future.done():
                break

            self.get_logger().info(
                f'Trying preset motion area={area_id}, motion={motion_id}... [{i}]')

        if not future.done():
            self.get_logger().error(
                f'Preset motion timed out: area={area_id}, motion={motion_id}')
            return False

        response = future.result()
        if response is None:
            self.get_logger().error(
                f'Preset motion failed: area={area_id}, motion={motion_id}')
            return False

        state = response.response.state.value
        code = response.response.header.code
        if code == 0 or state == CommonState.RUNNING:
            self.get_logger().info(
                f'Preset motion accepted: area={area_id}, motion={motion_id}, '
                f'task_id={response.response.task_id}')
            return True

        self.get_logger().error(
            f'Preset motion rejected: area={area_id}, motion={motion_id}, '
            f'task_id={response.response.task_id}, '
            'falling back to hand pose command')
        return False

    def build_hand_cmd(self, name, position):
        cmd = HandCommand()
        cmd.name = name
        cmd.position = float(position)
        cmd.velocity = 1.0
        cmd.acceleration = 1.0
        cmd.deceleration = 1.0
        cmd.effort = 1.0
        return cmd

    def publish_heart_hand_pose(self):
        msg = HandCommandArray()
        msg.header = MessageHeader()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.left_hand_type = HandType(value=2)
        msg.right_hand_type = HandType(value=2)
        msg.left_hands = [self.build_hand_cmd('left_hand', 0.5)]
        msg.right_hands = [self.build_hand_cmd('right_hand', 0.5)]

        self.hand_publisher.publish(msg)

    def hold_heart_hand_pose(self, duration_sec):
        start = self.get_clock().now()
        while (self.get_clock().now() - start).nanoseconds / 1e9 < duration_sec:
            self.publish_heart_hand_pose()
            rclpy.spin_once(self, timeout_sec=0.02)
            time.sleep(0.001)

    def play_heart_motion(self):
        self.get_logger().info('Playing heart preset motion')
        motion_ok = self.send_preset_motion(
            self.heart_area_id, self.heart_motion_id)
        self.hold_heart_hand_pose(3.0)
        return motion_ok

    def run_sequence(self):
        if not self.prepare_locomotion():
            return False

        self.get_logger().info('Walking forward for 3 seconds')
        self.publish_velocity_for(self.forward_velocity, 0.0, 0.0, 3.0)
        self.stop_motion()
        self.sleep_after_action()

        if not self.prepare_stand():
            return False
        self.play_heart_motion()
        self.stop_motion()
        self.sleep_after_action(self.heart_to_turn_sleep_sec)

        if not self.prepare_locomotion():
            return False
        turn_duration = (2.0 * math.pi) / self.turn_velocity
        self.get_logger().info(
            f'Turning 360 degrees for {turn_duration:.2f} seconds')
        self.publish_velocity_for(0.0, 0.0, self.turn_velocity, turn_duration)
        self.stop_motion()
        self.sleep_after_action()

        if not self.register_input_source():
            return False
        self.get_logger().info('Walking forward for 3 seconds')
        self.publish_velocity_for(self.forward_velocity, 0.0, 0.0, 3.0)
        self.stop_motion(1.0)

        self.get_logger().info('Sequence finished')
        return True


global_node = None


def signal_handler(sig, frame):
    global global_node
    if global_node is not None:
        global_node.stop_motion()
        global_node.get_logger().info(
            f'Received signal {sig}, stopping and shutting down')
    if rclpy.ok():
        rclpy.shutdown()
    sys.exit(0)


def main(args=None):
    global global_node
    rclpy.init(args=args)

    node = HeartWalkTurn()
    global_node = node

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        node.run_sequence()
    except KeyboardInterrupt:
        pass
    finally:
        node.stop_motion()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()


