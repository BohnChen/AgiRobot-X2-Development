#!/usr/bin/env python3

import base64
import io
import math
import json
import os
import re
import signal
import sys
import threading
import time

import rclpy
import rclpy.logging
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)

try:
    from py_examples.responder import generate_reply
except ImportError:
    try:
        from responder import generate_reply
    except ImportError:
        generate_reply = None

from aimdk_msgs.msg import (
    AudioData,
    AudioInfo,
    AudioPlayback,
    CommonState,
    FocusRequester,
    HandCommand,
    HandCommandArray,
    HandType,
    McActionCommand,
    McControlArea,
    McLocomotionVelocity,
    McPresetMotion,
    MessageHeader,
    ProcessedAudioOutput,
    RequestHeader,
)
from aimdk_msgs.srv import (
    AbandonAudioFocus,
    PlayEmoji,
    PlayTts,
    RequestAudioFocus,
    SetMcAction,
    SetMcInputSource,
    SetMcPresetMotion,
)


class ExamInvigilatorPatrol(Node):
    def __init__(self):
        super().__init__('exam_invigilator_patrol')

        self.velocity_publisher = self.create_publisher(
            McLocomotionVelocity, '/aima/mc/locomotion/velocity', 10)
        self.hand_publisher = self.create_publisher(
            HandCommandArray, '/aima/hal/joint/hand/command', 10)
        self.action_client = self.create_client(
            SetMcAction, '/aimdk_5Fmsgs/srv/SetMcAction')
        self.input_source_client = self.create_client(
            SetMcInputSource, '/aimdk_5Fmsgs/srv/SetMcInputSource')
        self.tts_client = self.create_client(
            PlayTts, '/aimdk_5Fmsgs/srv/PlayTts')
        self.focus_client = self.create_client(
            RequestAudioFocus, '/aimdk_5Fmsgs/srv/RequestAudioFocus')
        self.release_focus_client = self.create_client(
            AbandonAudioFocus, '/aimdk_5Fmsgs/srv/AbandonAudioFocus')
        self.audio_publisher = self.create_publisher(
            AudioPlayback, '/aima/hal/audio/playback', 10)
        self.emoji_client = self.create_client(
            PlayEmoji, '/face_ui_proxy/play_emoji')
        self.preset_motion_client = self.create_client(
            SetMcPresetMotion, '/aimdk_5Fmsgs/srv/SetMcPresetMotion')

        mic_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=100,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self.create_subscription(
            ProcessedAudioOutput,
            '/agent/process_audio_output',
            self.audio_callback,
            mic_qos)

        self.source = 'exam_invigilator'
        self.forward_velocity = 0.25
        self.turn_velocity = 0.7
        self.forward_duration = 2.0
        self.turn_degrees = 90.0
        self.stop_hold_sec = 1.0
        self.motion_settle_sec = 1.0
        self.locomotion_mode_settle_sec = 1.5
        self.exam_start_mode_settle_sec = 2.0
        self.preset_motion_settle_sec = 2.0
        self.action_retry_count = 3
        self.inspect_sleep_sec = 1.0
        self.input_source_timeout = 1000
        self.service_call_timeout = 8.0
        self.tts_max_wait_sec = 10.0
        self.tts_response_timeout = 10.0
        self.seed_tts_timeout = 8.0
        self.tts_service_ready = False
        self.last_tts_error = ''
        self.seed_tts_warned = False
        self.audio_focus_acquired = False
        self.hand_gesture_duration = 3.0
        self.tts_priority_level = 6
        self.tts_priority_weight = 0
        self.enable_taiwan_dialogue = False
        self.shutdown_requested = False
        self.shutting_down = False
        self.input_source_registered = False
        self.accepted_stream_id = 0
        self.use_command_grammar = False
        self.exam_start_grammar = [
            '监考开始',
            '现在监考开始',
            '确认开考',
            '进入监考模式',
            '开始考试',
            '考试开始',
            '开考',
            '[unk]',
        ]

        self.asr = None
        self.asr_model = None
        self.asr_recognizer_cls = None
        self.asr_ready = False
        self.asr_backend = ''
        self.audio_buffer = bytearray()
        self.is_recording = False
        self.waiting_for_exam_start = False
        self.exam_start_detected = False
        self.last_asr_text = ''
        self.asr_lock = threading.Lock()

        self.table_count = 3
        self.patrol_round = 0
        self.table_index = 0

        self.declare_parameter('vosk_model_dir', '')
        self.declare_parameter('accepted_stream_id', 0)
        self.declare_parameter('use_command_grammar', False)
        self.accepted_stream_id = self.get_parameter(
            'accepted_stream_id').value
        self.use_command_grammar = self.get_parameter(
            'use_command_grammar').value
        self.get_logger().info('Exam invigilator patrol node started')

    def load_asr(self):
        if self.asr_ready:
            return True

        self.get_logger().info('Loading Vosk ASR model for exam start command...')
        return self.load_vosk_asr()

    def load_vosk_asr(self):
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
        except ImportError:
            self.get_logger().error(
                'Vosk is not installed. Please install it: pip install vosk')
            return False

        model_dir = self.find_vosk_model_dir()
        if model_dir is None:
            self.get_logger().error('Vosk model directory not found')
            return False

        try:
            SetLogLevel(-1)
            self.asr_model = Model(model_dir)
            self.asr_recognizer_cls = KaldiRecognizer
        except Exception as e:
            self.get_logger().error(f'Failed to load Vosk model: {e}')
            return False

        self.asr_ready = True
        self.asr_backend = 'vosk'
        self.get_logger().info('Vosk ASR model loaded')
        return True

    def find_vosk_model_dir(self):
        candidates = []
        param_dir = self.get_parameter('vosk_model_dir').value
        env_dir = os.environ.get('VOSK_MODEL_DIR', '')
        if param_dir:
            candidates.append(param_dir)
        if env_dir:
            candidates.append(env_dir)

        candidates.append(os.path.join(
            os.getcwd(),
            'src',
            'x2_voice_chat',
            'models',
            'vosk-model-small-cn-0.22'))

        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(
            script_dir,
            '..',
            '..',
            '..',
            'src',
            'x2_voice_chat',
            'models',
            'vosk-model-small-cn-0.22'))
        candidates.append(os.path.join(
            script_dir,
            '..',
            '..',
            '..',
            '..',
            'src',
            'x2_voice_chat',
            'models',
            'vosk-model-small-cn-0.22'))
        candidates.append(os.path.join(
            script_dir,
            '..',
            '..',
            'x2_voice_chat',
            'models',
            'vosk-model-small-cn-0.22'))

        for path in candidates:
            model_dir = os.path.abspath(os.path.normpath(path))
            if os.path.isdir(model_dir):
                self.get_logger().info(f'Using Vosk model dir: {model_dir}')
                return model_dir

        self.get_logger().error(
            f'Tried Vosk model dirs: {", ".join(candidates)}')
        return None

    def clean_asr_text(self, text):
        text = re.sub(r'<\|[^|]+\|>', '', text)
        return text.replace(' ', '').strip()

    def audio_callback(self, msg):
        if not self.waiting_for_exam_start or not self.asr_ready:
            return

        if self.accepted_stream_id and msg.stream_id != self.accepted_stream_id:
            return

        vad = msg.audio_vad_state.value
        audio_data = bytes(msg.audio_data)

        if vad == 1:
            self.get_logger().info('Exam start voice detected')
            self.audio_buffer = bytearray(audio_data)
            self.is_recording = True
        elif vad == 2:
            if self.is_recording:
                self.audio_buffer.extend(audio_data)
        elif vad == 3:
            if self.is_recording:
                self.audio_buffer.extend(audio_data)
                audio_bytes = bytes(self.audio_buffer)
                self.is_recording = False
                self.get_logger().info(
                    f'Exam start voice ended: {len(audio_bytes)} bytes')
                threading.Thread(
                    target=self.process_exam_start_audio,
                    args=(audio_bytes,),
                    daemon=True).start()

    def process_exam_start_audio(self, audio_bytes):
        with self.asr_lock:
            if self.exam_start_detected:
                return

            try:
                if self.asr_backend == 'funasr':
                    result = self.asr.generate(input=audio_bytes)
                    raw_text = result[0].get('text', '')
                    text = self.clean_asr_text(raw_text)
                else:
                    if self.use_command_grammar:
                        try:
                            recognizer = self.asr_recognizer_cls(
                                self.asr_model,
                                16000,
                                json.dumps(
                                    self.exam_start_grammar,
                                    ensure_ascii=False))
                        except Exception:
                            recognizer = self.asr_recognizer_cls(
                                self.asr_model, 16000)
                    else:
                        recognizer = self.asr_recognizer_cls(
                            self.asr_model, 16000)
                    recognizer.AcceptWaveform(audio_bytes)
                    raw_text = json.loads(recognizer.FinalResult()).get('text', '')
                    text = self.clean_asr_text(raw_text)
            except Exception as e:
                self.get_logger().error(f'ASR error: {e}')
                return

            if not text:
                self.get_logger().info(
                    f'Exam start ASR received audio but got empty text: '
                    f'{len(audio_bytes)} bytes')
                return

            self.last_asr_text = text
            self.get_logger().info(f'Exam start ASR: {text}')
            if self.is_exam_start_command(text):
                self.exam_start_detected = True

    def is_exam_start_command(self, text):
        normalized = text.replace('，', '').replace('。', '').replace('！', '')
        start_words = (
            '监考开始',
            '監考開始',
            '后市开始',
            '後市開始',
            '总是开始',
            '總是開始',
            '确认开考',
            '確認開考',
            '进入监考模式',
            '進入監考模式',
            '开始考试',
            '開始考試',
            '考试开始',
            '考試開始',
            '开考',
            '開考',
        )
        return any(word in normalized for word in start_words)

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

    def call_with_retry(self, client, req, request_stamp, label):
        request_stamp()
        future = client.call_async(req)
        rclpy.spin_until_future_complete(
            self, future, timeout_sec=self.service_call_timeout)

        if not future.done():
            self.get_logger().error(f'{label} timed out')
            return None

        response = future.result()
        if response is None:
            self.get_logger().error(f'{label} failed')
            return None

        return response

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

        response = self.call_with_retry(
            self.action_client,
            req,
            lambda: setattr(req.header, 'stamp',
                            self.get_clock().now().to_msg()),
            f'set MC action {action_name}')
        if response is None:
            return False

        if response.response.status.value == CommonState.SUCCESS:
            self.get_logger().info(f'MC action set: {action_name}')
            return True

        self.get_logger().error(
            f'MC action rejected: {action_name}, '
            f'message={response.response.message}')
        return False

    def register_input_source(self, action_value):
        if not self.wait_for_service(
                self.input_source_client,
                '/aimdk_5Fmsgs/srv/SetMcInputSource'):
            return False

        req = SetMcInputSource.Request()
        req.action.value = action_value
        req.input_source.name = self.source
        req.input_source.priority = 40
        req.input_source.timeout = self.input_source_timeout

        response = self.call_with_retry(
            self.input_source_client,
            req,
            lambda: setattr(req.request.header, 'stamp',
                            self.get_clock().now().to_msg()),
            f'set input source action={action_value}')
        if response is None:
            return False

        code = response.response.header.code
        state = response.response.state.value
        if code != 0:
            message = (
                f'Input source rejected: action={action_value}, '
                f'code={code}, state={state}, '
                f'task_id={response.response.task_id}')
            if action_value == 1001:
                self.get_logger().warn(message)
            else:
                self.get_logger().error(message)
            return False

        self.input_source_registered = True
        self.get_logger().info(
            f'Input source set: state={state}, '
            f'task_id={response.response.task_id}')
        return True

    def prepare_locomotion(self):
        for i in range(self.action_retry_count):
            if self.shutdown_requested:
                return False
            if self.set_mc_action('LOCOMOTION_DEFAULT'):
                time.sleep(1.0)
                action_values = (1001, 1002)
                if self.input_source_registered:
                    action_values = (1002,)
                for action_value in action_values:
                    if self.register_input_source(action_value):
                        self.publish_velocity_for(
                            0.0, 0.0, 0.0,
                            self.locomotion_mode_settle_sec)
                        return True
                return False

            self.get_logger().warn(
                f'LOCOMOTION_DEFAULT rejected, settling before retry [{i}]')
            self.stop_motion(1.0)
            time.sleep(self.motion_settle_sec)

        return False

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
            if (self.shutdown_requested and
                    (forward != 0.0 or lateral != 0.0 or angular != 0.0)):
                break
            self.publish_velocity(forward, lateral, angular)
            rclpy.spin_once(self, timeout_sec=0.02)
            time.sleep(0.001)

    def stop_motion(self, hold_sec=None):
        if hold_sec is None:
            hold_sec = self.stop_hold_sec

        self.get_logger().info('Stopping locomotion')
        self.publish_velocity_for(0.0, 0.0, 0.0, hold_sec)
        time.sleep(self.motion_settle_sec)

    def build_hand_cmd(self, name, position):
        cmd = HandCommand()
        cmd.name = name
        cmd.position = float(position)
        cmd.velocity = 1.0
        cmd.acceleration = 1.0
        cmd.deceleration = 1.0
        cmd.effort = 1.0
        return cmd

    def publish_hand_pose(self, left_position, right_position):
        msg = HandCommandArray()
        msg.header = MessageHeader()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.left_hand_type = HandType(value=2)
        msg.right_hand_type = HandType(value=2)
        msg.left_hands = [self.build_hand_cmd('left_hand', left_position)]
        msg.right_hands = [self.build_hand_cmd('right_hand', right_position)]
        self.hand_publisher.publish(msg)

    def publish_item_hand_gesture(self, gesture):
        if gesture == 'phone':
            left_position = 0.0
            right_position = 1.0
        elif gesture == 'book':
            left_position = 0.5
            right_position = 0.5
        else:
            left_position = 0.0
            right_position = 0.0

        start = self.get_clock().now()
        while (self.get_clock().now() - start).nanoseconds / 1e9 < \
                self.hand_gesture_duration:
            if self.shutdown_requested:
                break
            self.publish_hand_pose(left_position, right_position)
            time.sleep(0.05)

    def start_item_hand_gesture(self, gesture):
        thread = threading.Thread(
            target=self.publish_item_hand_gesture,
            args=(gesture,),
            daemon=True)
        thread.start()

    def walk_to_next_table(self):
        if not self.prepare_locomotion():
            return False

        self.get_logger().info(
            f'Walking forward for {self.forward_duration:.1f} seconds')
        self.publish_velocity_for(
            self.forward_velocity, 0.0, 0.0, self.forward_duration)
        self.stop_motion()
        return True

    def turn_left_90(self):
        if not self.prepare_locomotion():
            return False

        turn_duration = math.radians(self.turn_degrees) / self.turn_velocity
        self.get_logger().info(
            f'Turning {self.turn_degrees:.0f} degrees for '
            f'{turn_duration:.2f} seconds')
        self.publish_velocity_for(
            0.0, 0.0, self.turn_velocity, turn_duration)
        self.stop_motion()
        return True

    def ensure_tts_service(self):
        if self.tts_service_ready:
            return True

        self.tts_service_ready = self.wait_for_service(
            self.tts_client, '/aimdk_5Fmsgs/srv/PlayTts')
        return self.tts_service_ready

    def play_tts(self, text):
        self.last_tts_error = ''
        if not self.ensure_tts_service():
            return None

        req = PlayTts.Request()
        req.tts_req.text = text
        req.tts_req.domain = 'exam_invigilator'
        req.tts_req.trace_id = 'exam_patrol'
        req.tts_req.is_interrupted = True
        req.tts_req.priority_weight = self.tts_priority_weight
        req.tts_req.priority_level.value = self.tts_priority_level

        req.header.header.stamp = self.get_clock().now().to_msg()
        future = self.tts_client.call_async(req)
        rclpy.spin_until_future_complete(
            self, future, timeout_sec=self.tts_response_timeout)

        if not future.done():
            self.get_logger().error(f'PlayTts timed out: {text}')
            return None

        response = future.result()
        if response is None:
            self.get_logger().error(f'PlayTts failed: {text}')
            return None

        if response.tts_resp.is_success:
            self.get_logger().info(f'TTS sent: {text}')
            return response.tts_resp.estimated_duration

        self.last_tts_error = response.tts_resp.error_message
        self.get_logger().error(
            f'TTS rejected: {text}, '
            f'error={response.tts_resp.error_message}, '
            f'priority={response.tts_resp.priority_level.value}, '
            f'weight={response.tts_resp.priority_weight}')
        return None

    def play_tts_and_wait(self, text):
        estimated_duration = self.play_tts(text)
        if estimated_duration is None:
            return False

        if estimated_duration == 0:
            wait_sec = len(text) * 0.25
        else:
            wait_sec = estimated_duration / 1000.0

        wait_sec = min(wait_sec + 0.5, self.tts_max_wait_sec)
        self.get_logger().info(f'Waiting {wait_sec:.1f} seconds for TTS')
        time.sleep(wait_sec)
        return True

    def play_tts_optional(self, text):
        if self.play_seed_tts_and_wait(text):
            return True

        if self.play_tts_and_wait(text):
            return True

        self.get_logger().warn(f'TTS skipped, continuing: {text}')
        return False

    def play_local_tts_optional(self, text):
        if self.play_tts_and_wait(text):
            return True

        self.get_logger().warn(f'Local TTS skipped, continuing: {text}')
        return False

    def play_seed_tts_and_wait(self, text):
        self.get_logger().info(f'Requesting Seed TTS: {text}')
        pcm_data = self.synthesize_seed_tts(text)
        if not pcm_data:
            return False

        wait_sec = self.publish_seed_audio(pcm_data)
        self.wait_for_audio_playback(wait_sec)
        return True

    def publish_seed_audio(self, pcm_data):
        self.request_audio_focus()

        msg = AudioPlayback()
        msg.stamps = self.get_clock().now().to_msg()
        msg.info = AudioInfo()
        msg.info.channels = 1
        msg.info.sample_rate = 16000
        msg.info.size = len(pcm_data)
        msg.info.sample_format = 'S16_LE'
        msg.info.coding_format = 'pcm'
        msg.data = AudioData()
        msg.data.data = pcm_data
        msg.pkg_name = self.source
        msg.token_id = f'{self.source}_{int(time.time() * 1000)}'
        self.audio_publisher.publish(msg)

        wait_sec = min(len(pcm_data) / 32000.0 + 0.5, self.tts_max_wait_sec)
        self.get_logger().info(
            f'Seed TTS audio published, waiting {wait_sec:.1f} seconds')
        return wait_sec

    def wait_for_audio_playback(self, wait_sec):
        end_time = time.time() + wait_sec
        while time.time() < end_time and rclpy.ok():
            rclpy.spin_once(self, timeout_sec=0.05)
        self.release_audio_focus()

    def synthesize_seed_tts(self, text):
        api_key = os.environ.get('SEED_API_KEY', '')
        if not api_key:
            if not self.seed_tts_warned:
                self.get_logger().warn(
                    'SEED_API_KEY is not set; Seed TTS fallback disabled')
                self.seed_tts_warned = True
            return None

        try:
            from pydub import AudioSegment
        except ImportError:
            self.get_logger().error(
                'pydub is not installed; Seed TTS fallback needs pydub')
            return None

        voice_prompt = (
            '年轻台湾女性监考老师，必须使用明显台湾国语口音，'
            '不是大陆普通话播音腔，语气亲切自然，尾音轻柔，'
            '语速稍慢，像台北老师在考场温和提醒学生')
        request_json = {
            'model': 'seed-audio-1.0',
            'text_prompt': f'{voice_prompt}：“{text}”',
            'audio_config': {
                'format': 'mp3',
                'sample_rate': 24000,
                'pitch_rate': 0,
                'speech_rate': 0,
                'loudness_rate': 0,
            },
            'watermark': {},
        }

        try:
            import urllib.request
            data = json.dumps(request_json).encode('utf-8')
            req = urllib.request.Request(
                'https://openspeech.bytedance.com/api/v3/tts/create',
                data=data,
                headers={
                    'Content-Type': 'application/json',
                    'X-Api-Key': api_key,
                })
            with urllib.request.urlopen(req, timeout=self.seed_tts_timeout) as resp:
                body = json.loads(resp.read())

            if 'code' in body and body['code'] != 80000:
                message = body.get('message', body.get('msg', 'unknown'))
                self.get_logger().error(f'Seed Audio error: {message}')
                return None

            audio_b64 = body.get('audio', '')
            if not audio_b64 and 'data' in body:
                audio_b64 = body['data'].get('audio', '')
            if not audio_b64:
                self.get_logger().error('Seed Audio returned empty audio')
                return None

            mp3_data = base64.b64decode(audio_b64)
            audio = AudioSegment.from_mp3(io.BytesIO(mp3_data))
            audio = audio.set_frame_rate(16000).set_channels(1)
            self.get_logger().info('Seed Audio synthesized')
            return audio.raw_data
        except Exception as e:
            self.get_logger().error(f'Seed Audio TTS error: {e}')
            return None

    def request_audio_focus(self):
        if self.audio_focus_acquired:
            return True
        if not self.focus_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().warn('Audio focus service unavailable')
            return False

        req = RequestAudioFocus.Request()
        req.request.header.stamp = self.get_clock().now().to_msg()
        req.focus_requester = FocusRequester()
        req.focus_requester.pkg_name = self.source
        req.focus_requester.priority = 10
        req.focus_requester.priority_weight = 0
        future = self.focus_client.call_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=3.0)
        if not future.done() or future.result() is None:
            self.get_logger().warn('Audio focus request timed out')
            return False

        response = future.result()
        self.audio_focus_acquired = response.focus_response.focus_gain
        return self.audio_focus_acquired

    def release_audio_focus(self):
        if not self.audio_focus_acquired:
            return
        if not self.release_focus_client.wait_for_service(timeout_sec=1.0):
            return

        req = AbandonAudioFocus.Request()
        req.request.header.stamp = self.get_clock().now().to_msg()
        req.focus_requester = FocusRequester()
        req.focus_requester.pkg_name = self.source
        req.focus_requester.priority = 10
        req.focus_requester.priority_weight = 0
        future = self.release_focus_client.call_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=2.0)
        self.audio_focus_acquired = False

    def play_emoji(self, emoji_id, priority=80):
        if not self.wait_for_service(self.emoji_client,
                                     '/face_ui_proxy/play_emoji'):
            return False

        req = PlayEmoji.Request()
        req.emotion_id = int(emoji_id)
        req.mode = 1
        req.priority = priority

        response = self.call_with_retry(
            self.emoji_client,
            req,
            lambda: setattr(req.header.header, 'stamp',
                            self.get_clock().now().to_msg()),
            f'play emoji {emoji_id}')
        if response is None:
            return False

        if response.success:
            self.get_logger().info(f'Emoji played: {emoji_id}')
            return True

        self.get_logger().error(
            f'Emoji rejected: {emoji_id}, message={response.message}')
        return False

    def play_emoji_async(self, emoji_id, priority=100):
        if not self.emoji_client.wait_for_service(timeout_sec=0.5):
            self.get_logger().warn('Emoji service unavailable')
            return False

        req = PlayEmoji.Request()
        req.emotion_id = int(emoji_id)
        req.mode = 1
        req.priority = priority
        req.header.header.stamp = self.get_clock().now().to_msg()
        future = self.emoji_client.call_async(req)

        def on_done(done_future):
            try:
                response = done_future.result()
            except Exception as exc:
                self.get_logger().warn(
                    f'Emoji async exception: {emoji_id}, error={exc}')
                return

            if response is None:
                self.get_logger().warn(f'Emoji async failed: {emoji_id}')
            elif response.success:
                self.get_logger().info(f'Emoji async played: {emoji_id}')
            else:
                self.get_logger().warn(
                    f'Emoji async rejected: {emoji_id}, '
                    f'message={response.message}')

        future.add_done_callback(on_done)
        return True

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

        response = self.call_with_retry(
            self.preset_motion_client,
            req,
            lambda: setattr(req.header, 'stamp',
                            self.get_clock().now().to_msg()),
            f'preset motion area={area_id}, motion={motion_id}')
        if response is None:
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
            f'task_id={response.response.task_id}')
        return False

    def send_preset_motion_async(self, area_id, motion_id):
        if not self.preset_motion_client.wait_for_service(timeout_sec=0.5):
            self.get_logger().warn('Preset motion service unavailable')
            return False

        req = SetMcPresetMotion.Request()
        req.header = RequestHeader()
        req.header.stamp = self.get_clock().now().to_msg()
        req.motion = McPresetMotion(value=motion_id)
        req.area = McControlArea(value=area_id)
        req.interrupt = False
        future = self.preset_motion_client.call_async(req)

        def on_done(done_future):
            try:
                response = done_future.result()
            except Exception as exc:
                self.get_logger().warn(
                    f'Preset motion async exception: area={area_id}, '
                    f'motion={motion_id}, error={exc}')
                return

            if response is None:
                self.get_logger().warn(
                    f'Preset motion async failed: area={area_id}, '
                    f'motion={motion_id}')
                return

            state = response.response.state.value
            code = response.response.header.code
            if code == 0 or state == CommonState.RUNNING:
                self.get_logger().info(
                    f'Preset motion async accepted: area={area_id}, '
                    f'motion={motion_id}, task_id={response.response.task_id}')
            else:
                self.get_logger().warn(
                    f'Preset motion async rejected: area={area_id}, '
                    f'motion={motion_id}, task_id={response.response.task_id}')

        future.add_done_callback(on_done)
        return True

    def read_detected_item(self, table_id):
        while rclpy.ok() and not self.shutdown_requested:
            try:
                raw = input(
                    f'请输入 {table_id} 号桌检测结果 phone(1)/book(2)/none(3): '
                ).strip().lower()
            except KeyboardInterrupt:
                self.shutdown_requested = True
                raise
            except EOFError:
                self.get_logger().error('Input closed while reading table item')
                return None

            if raw in ('1', 'phone'):
                return 'phone'
            if raw in ('2', 'book'):
                return 'book'
            if raw in ('3', 'none'):
                return 'none'

            print('输入无效，请输入 phone(1)、book(2) 或 none(3)。')

        return None

    def generate_taiwan_reply(self, user_text):
        if generate_reply is None:
            return '歹勢，我現在沒有辦法回覆你，不過我有在聽喔。'

        return generate_reply(user_text)

    def run_taiwan_dialogue(self, table_id):
        if not self.enable_taiwan_dialogue:
            return

        user_text = input(
            f'请输入 {table_id} 号桌考生想对机器人说的话'
            '(直接回车跳过台湾腔对话): ').strip()
        if not user_text:
            return

        reply = self.generate_taiwan_reply(user_text)
        self.get_logger().info(f'Taiwan reply: {reply}')
        self.play_tts_and_wait(reply)

    def play_item_effects(self, text, emoji_id, area_id, motion_id, label):
        self.play_emoji(emoji_id, priority=100)
        if not self.send_preset_motion(area_id, motion_id):
            self.get_logger().warn(
                f'Preset motion skipped for {label}, '
                'falling back to hand pose')
            self.start_item_hand_gesture(label)
        time.sleep(self.preset_motion_settle_sec)

        pcm_data = self.synthesize_seed_tts(text)
        if pcm_data:
            wait_sec = self.publish_seed_audio(pcm_data)
            self.wait_for_audio_playback(wait_sec)
            return True

        self.get_logger().warn(
            f'Seed TTS unavailable for {label}, trying system TTS')
        if self.play_tts_and_wait(text):
            return True

        self.get_logger().warn(f'Item TTS skipped for {label}: {text}')
        return False

    def wait_for_exam_start(self):
        if not self.prepare_stand():
            return False
        if not self.load_asr():
            return False

        self.exam_start_detected = False
        self.last_asr_text = ''

        prompt_ok = self.play_local_tts_optional(
            '請問現在要進入監考模式了嗎？準備好了的話，請說監考開始喔。')
        if not prompt_ok:
            self.get_logger().warn(
                'Start prompt TTS was not played. Please say: 监考开始')
            time.sleep(1.0)
        self.waiting_for_exam_start = True
        self.get_logger().info(
            'Waiting for voice command: 监考开始 / 确认开考 / 进入监考模式')

        while (rclpy.ok() and not self.exam_start_detected and
               not self.shutdown_requested):
            rclpy.spin_once(self, timeout_sec=0.1)

        self.waiting_for_exam_start = False
        if not self.exam_start_detected:
            return False

        if not self.prepare_stand():
            return False
        time.sleep(self.exam_start_mode_settle_sec)
        self.play_local_tts_optional('考試開始囉，請各位同學遵守考試規則，好好作答喔。')
        return True

    def respond_to_item(self, table_id, item):
        if not self.prepare_stand():
            return False

        if item == 'phone':
            self.get_logger().info(
                f'Table {table_id}: phone detected, severe violation')
            self.play_item_effects(
                f'{table_id}號桌同學，我有看到手機喔，'
                '考試的時候不能使用手機，麻煩你現在立刻收起來。',
                190,
                2,
                1001,
                'phone')
        elif item == 'book':
            self.get_logger().info(
                f'Table {table_id}: book detected, reminder needed')
            self.play_item_effects(
                f'{table_id}號桌同學，桌上有書本耶，'
                '請先把跟考試無關的物品收起來，謝謝喔。',
                70,
                2,
                1002,
                'book')
        else:
            self.get_logger().info(f'Table {table_id}: normal')
            self.play_item_effects(
                f'{table_id}號桌檢查正常，請繼續安心作答喔。',
                90,
                4,
                4001,
                'normal')

        time.sleep(self.inspect_sleep_sec)
        self.run_taiwan_dialogue(table_id)
        return True

    def inspect_table(self):
        table_id = self.table_index + 1
        item = self.read_detected_item(table_id)
        if item is None:
            return False
        return self.respond_to_item(table_id, item)

    def run_sequence(self):
        if not self.wait_for_exam_start():
            return False

        while rclpy.ok() and not self.shutdown_requested:
            self.patrol_round += 1
            self.get_logger().info(f'Starting patrol round {self.patrol_round}')

            for self.table_index in range(self.table_count):
                if self.shutdown_requested:
                    return False
                if not self.walk_to_next_table():
                    return False
                if not self.inspect_table():
                    return False
                if not self.turn_left_90():
                    return False

            self.get_logger().info(
                f'Patrol round {self.patrol_round} finished')

        return True


global_node = None


def signal_handler(sig, frame):
    """Signal handler only marks shutdown; ROS calls stay in normal flow."""
    global global_node
    if global_node is not None:
        global_node.shutdown_requested = True
    raise KeyboardInterrupt


def main(args=None):
    global global_node
    rclpy.init(args=args)

    node = ExamInvigilatorPatrol()
    global_node = node

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    exit_code = 1
    try:
        if node.run_sequence():
            exit_code = 0
    except KeyboardInterrupt:
        exit_code = 130
    except Exception as e:
        rclpy.logging.get_logger('main').error(
            f'Program exited with exception: {e}')
        exit_code = 1
    finally:
        if not node.shutting_down and rclpy.ok():
            try:
                node.stop_motion()
                node.release_audio_focus()
            except KeyboardInterrupt:
                pass
            finally:
                node.shutting_down = True
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
