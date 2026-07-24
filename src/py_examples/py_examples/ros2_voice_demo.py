#!/usr/bin/env python3
"""
ros2_voice_demo.py — 灵犀X2 台湾腔语音对话 · ROS2 启动入口

这个文件是一个"启动器"，解决 ros2 run 找不到虚拟环境依赖的问题。
实际逻辑在 taiwan_voice.py 和 responder.py 中。

使用:
  python3 ~/aimdk/src/py_examples/py_examples/ros2_voice_demo.py

或通过 shell wrapper + ros2 run（见下方说明）
"""

from py_examples.taiwan_voice import main

if __name__ == '__main__':
    main()
