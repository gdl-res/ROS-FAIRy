"""Grab /robot_description and /tf_static via a minimal rclpy node.

This is the only harvest module allowed to use rclpy (CLAUDE.md principle 5):
both topics are transient-local latched publishers that subprocess tooling
cannot read reliably. Hard 5-second budget; returns Nones on any problem,
including rclpy not being importable at all.
"""

import time
from typing import Any

RCLPY_TIMEOUT_S = 5


def harvest(timeout_s: float = RCLPY_TIMEOUT_S) -> dict[str, Any]:
    """Return {robot_description: str|None, tf_static: list[dict]|None}."""
    result: dict[str, Any] = {"robot_description": None, "tf_static": None}
    try:
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import (
            DurabilityPolicy,
            HistoryPolicy,
            QoSProfile,
            ReliabilityPolicy,
        )
        from std_msgs.msg import String
        from tf2_msgs.msg import TFMessage
    except ImportError:
        return result

    latched = QoSProfile(
        depth=1,
        history=HistoryPolicy.KEEP_LAST,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )

    context = None
    try:
        context = rclpy.Context()
        rclpy.init(context=context)
        node = rclpy.create_node("fair_ros_harvest", context=context)
        # A private context needs its own executor: the module-level
        # rclpy.spin_once() would use the global executor bound to the
        # (uninitialised) default context.
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)

        def on_urdf(msg):
            result["robot_description"] = msg.data

        def on_tf(msg):
            result["tf_static"] = [_transform_to_dict(t) for t in msg.transforms]

        node.create_subscription(String, "/robot_description", on_urdf, latched)
        node.create_subscription(TFMessage, "/tf_static", on_tf, latched)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.1)
            if result["robot_description"] is not None and \
                    result["tf_static"] is not None:
                break
        executor.shutdown()
        node.destroy_node()
    except Exception:
        pass
    finally:
        if context is not None:
            try:
                rclpy.shutdown(context=context)
            except Exception:
                pass
    return result


def _transform_to_dict(t) -> dict[str, Any]:
    tr, rot = t.transform.translation, t.transform.rotation
    return {
        "parent_frame": t.header.frame_id,
        "child_frame": t.child_frame_id,
        "translation": {"x": tr.x, "y": tr.y, "z": tr.z},
        "rotation": {"x": rot.x, "y": rot.y, "z": rot.z, "w": rot.w},
    }
