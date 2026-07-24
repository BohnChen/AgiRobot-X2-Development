# AimDK 项目协作记忆

## 交流规则
- 用户要求全程使用中文交流。
- 当前项目路径：`E:\aimdk`，这是灵犀 X2 机器人的 AimDK SDK 项目。
- 修改范围仅限当前文件夹 `E:\aimdk`；如需改当前文件夹外文件，必须先向用户申请。
- 修改项目文件前必须先备份对应文件，保证可回滚。
- 当前目录不是 Git 仓库，不能依赖 Git 回滚；使用 `.codex_backups/` 下的文件备份回滚。
- 不允许删除当前目录外文件。删除当前目录内文件前也要明确删除范围和影响。
- 修改代码要遵守原项目编码规范，避免影响现有功能。

## 项目结构概览
- `README.md` / `README.en.md`：SDK 使用说明。
- `topics_and_services`：话题和服务列表。
- `version`：当前版本 `aimdk v1.0.0-ga424add`。
- `src/aimdk_msgs`：ROS 2 消息与服务接口，含预编译产物。
- `src/examples`：C++ 示例，ament_cmake 工程。
- `src/py_examples`：Python 示例，ament_python 工程。
- `src/ruckig`：轨迹规划库。
- `extra/x2_rl_deploy`：RL 部署示例，含控制器、Mujoco 仿真资源和 ONNX 配置。
- `docs`：本地 HTML 文档。

## 环境与验证情况
- 当前路径没有执行过 `colcon build`，不影响读取和修改源码。
- 未运行任何会控制机器人的 ROS 节点。
- 多次使用 `python -m py_compile` 做 Python 语法检查，生成的对应 `__pycache__/*.pyc` 验证副产物已删除。
- 当前 Windows 沙箱 helper 多次出现权限/启动失败，因此部分读取、备份和写入命令使用了用户批准的非沙箱 PowerShell 命令。

## 已新增或修改的节点

### 1. `heart_walk_turn`
- 文件：`src/py_examples/py_examples/heart_walk_turn.py`
- 入口已注册到：`src/py_examples/setup.py`
- 运行方式：`ros2 run py_examples heart_walk_turn`
- 功能顺序：
  1. 注册运动输入源。
  2. 前进 3 秒。
  3. 执行双手比心动作。
  4. 原地转身 180 度。
  5. 再前进 3 秒。
  6. 停止。
- 用户补充的官方动作参数：`motion_id=1007`，`area_id=3`。
- 备份：
  - `E:\aimdk\.codex_backups\20260722_heart_walk_turn\setup.py.bak`
  - `E:\aimdk\.codex_backups\20260722_heart_walk_turn\heart_walk_turn.py.bak`

### 2. `input_locomotion_velocity`
- 文件：`src/py_examples/py_examples/input_locomotion_velocity.py`
- 入口已注册到：`src/py_examples/setup.py`
- 运行方式：`ros2 run py_examples input_locomotion_velocity`
- 功能：
  - 输入 `forward velocity`。
  - 输入 `lateral velocity`。
  - 输入 `angular velocity`。
  - 输入 `duration`。
  - 按输入速度运行指定时长，到时停止。
  - 中断或异常时发布停止速度。
- 基于：`src/py_examples/py_examples/mc_locomotion_velocity.py`。
- 备份：
  - `E:\aimdk\.codex_backups\20260722_input_locomotion_velocity\setup.py.bak`

### 3. `nearest_lidar_object`
- 文件：`src/py_examples/py_examples/nearest_lidar_object.py`
- 入口已注册到：`src/py_examples/setup.py`
- 运行方式：`ros2 run py_examples nearest_lidar_object`
- 订阅话题：`/aima/hal/sensor/lidar_chest_front/lidar_pointcloud`
- 消息类型：`sensor_msgs/msg/PointCloud2`
- 功能：
  - 解析 `x/y/z` 字段。
  - 跳过 `NaN`、`Inf` 和零距离点。
  - 每秒输出最近有效点距离，单位米。
- 备份：
  - `E:\aimdk\.codex_backups\20260722_nearest_lidar_object\setup.py.bak`

### 4. `rear_camera_undistort`
- 文件：`src/py_examples/py_examples/rear_camera_undistort.py`
- 入口已注册到：`src/py_examples/setup.py`
- 运行方式：`ros2 run py_examples rear_camera_undistort`
- 订阅压缩图像：`/aima/hal/sensor/rgb_head_rear/rgb_image/compressed`
- 订阅相机内参：`/aima/hal/sensor/rgb_head_rear/camera_info`
- 发布校正后压缩图像：`/aima/hal/sensor/rgb_head_rear/rgb_image/undistorted/compressed`
- 功能：
  - 使用 `CameraInfo.k` 和 `CameraInfo.d` 生成去畸变映射。
  - 支持 `fisheye/equidistant` 以及普通畸变模型。
  - 使用 OpenCV 解码、去畸变、重新编码 JPEG。
- 可调参数示例：
  - `alpha`：默认 `0.0`。
  - `jpeg_quality`：默认 `90`。
- 运行示例：
  - `ros2 run py_examples rear_camera_undistort --ros-args -p alpha:=0.0 -p jpeg_quality:=90`
- 备份：
  - `E:\aimdk\.codex_backups\20260722_rear_camera_undistort\setup.py.bak`

## `setup.py` 已新增入口汇总
位于 `src/py_examples/setup.py` 的 `console_scripts` 中：
- `heart_walk_turn = py_examples.heart_walk_turn:main`
- `input_locomotion_velocity = py_examples.input_locomotion_velocity:main`
- `nearest_lidar_object = py_examples.nearest_lidar_object:main`
- `rear_camera_undistort = py_examples.rear_camera_undistort:main`

## 后续建议
- 在机器人或 ROS 2 Humble 环境中执行 `colcon build` 后再运行节点。
- 如果运行失败，优先检查：
  - `setup.py` 入口是否构建安装成功。
  - 对应话题是否存在。
  - `CameraInfo` 是否能被 `TRANSIENT_LOCAL` QoS 接收到。
  - OpenCV、numpy、sensor_msgs 等依赖是否存在。
- 若要回滚本轮新增节点：删除对应新增 `.py` 文件，并用相应 `.codex_backups/.../setup.py.bak` 覆盖 `src/py_examples/setup.py`。
