"""The brain owns when idle runs; claim_head syncs the pose before any move."""

import asyncio
import json
import unittest
from unittest.mock import patch

from brain import config, main
from brain.idle import IdleController, IdleInputs
from brain.tracker import Tracker


def quiet(**over) -> IdleInputs:
    base = dict(connected=True, awake=True, thinking=False, speaking=False,
                hearing=False, tracking=False, tracking_enabled=False, held=False)
    base.update(over)
    return IdleInputs(**base)


class IdleControllerTests(unittest.TestCase):
    def test_waits_a_full_quiet_period(self):
        ctl = IdleController(8.0)
        self.assertFalse(ctl.wanted(0.0, quiet()))
        self.assertFalse(ctl.wanted(7.9, quiet()))
        self.assertTrue(ctl.wanted(8.0, quiet()))

    def test_each_blocker_restarts_the_quiet_clock(self):
        for name in ("thinking", "speaking", "hearing", "tracking", "tracking_enabled"):
            ctl = IdleController(8.0)
            ctl.wanted(0.0, quiet())
            self.assertFalse(ctl.wanted(5.0, quiet(**{name: True})), name)
            self.assertFalse(ctl.wanted(12.9, quiet()), name)
            self.assertTrue(ctl.wanted(13.0, quiet()), name)
        ctl = IdleController(8.0)
        self.assertFalse(ctl.wanted(0.0, quiet(connected=False)))
        self.assertFalse(ctl.wanted(1.0, quiet(awake=False)))
        self.assertEqual(ctl.reason, "asleep")

    def test_a_still_tracked_face_keeps_idle_off(self):
        ctl = IdleController(8.0)
        for t in range(0, 60):
            self.assertFalse(ctl.wanted(float(t), quiet(tracking=True)))
        self.assertEqual(ctl.reason, "tracking")

    def test_tracking_enabled_without_a_face_keeps_idle_off(self):
        ctl = IdleController(8.0)
        for t in range(0, 60):
            self.assertFalse(ctl.wanted(float(t), quiet(tracking_enabled=True)))
        self.assertEqual(ctl.reason, "tracking")

    def test_a_held_look_resumes_when_the_hold_ends(self):
        ctl = IdleController(8.0)
        ctl.note_activity(0.0)
        self.assertFalse(ctl.wanted(20.0, quiet(held=True)))
        self.assertEqual(ctl.reason, "holding a look")
        self.assertTrue(ctl.wanted(30.0, quiet()))

    def test_activity_restarts_the_clock(self):
        ctl = IdleController(8.0)
        ctl.wanted(0.0, quiet())
        ctl.note_activity(6.0)
        self.assertFalse(ctl.wanted(13.0, quiet()))
        self.assertTrue(ctl.wanted(14.0, quiet()))


class FakeRobot:
    """Answers each glance-with-id with an idle_ack carrying `pose`."""

    def __init__(self, pose=(12.5, -7.0), answer=True):
        self.sent = []
        self.pose = pose
        self.answer = answer

    async def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if self.answer and msg.get("type") == "glance" and "id" in msg:
            pan, tilt = self.pose
            asyncio.get_running_loop().call_soon(main._resolve_ack, {
                "type": "idle_ack", "id": msg["id"], "idle": msg["on"], "pan": pan, "tilt": tilt,
            })


class HeadOwnershipBase(unittest.TestCase):
    def setUp(self):
        self._saved = (main.robot_socket, main.tracker, main.idle_sent, main.pose_synced,
                       main.head_held_until, main.idle_ctl, main._ack_retry_at)
        self._debug = config.IDLE_DEBUG
        config.IDLE_DEBUG = False
        main.tracker = Tracker(None, lambda *a: None)
        main.robot_socket = None
        main._reset_head_ownership()
        main.idle_ctl = IdleController(config.IDLE_QUIET_SECONDS)

    def tearDown(self):
        (main.robot_socket, main.tracker, main.idle_sent, main.pose_synced,
         main.head_held_until, main.idle_ctl, main._ack_retry_at) = self._saved
        config.IDLE_DEBUG = self._debug
        main._ack_waiters.clear()

    def glances(self, robot):
        return [m for m in robot.sent if m.get("type") == "glance"]


class IdleInputsTests(HeadOwnershipBase):
    def test_enabled_tracking_blocks_idle_before_a_face(self):
        main.robot_socket = object()
        main.tracker.enabled = True
        main.tracker.tracking = False
        with patch.object(main, "staying_up", return_value=True):
            state = main.idle_inputs(0.0)
        self.assertTrue(state.tracking_enabled)
        self.assertFalse(state.tracking)
        ctl = IdleController(8.0)
        self.assertFalse(ctl.wanted(8.0, state))
        self.assertEqual(ctl.reason, "tracking")


class ClaimHeadTests(HeadOwnershipBase):
    def test_claim_reads_both_axes_from_the_firmware(self):
        robot = FakeRobot(pose=(12.5, -7.0))
        main.robot_socket = robot
        main.idle_sent = True

        async def run():
            async with main.claim_head():
                return main.tracker.pan, main.tracker.tilt

        self.assertEqual(asyncio.run(run()), (12.5, -7.0))
        self.assertEqual(self.glances(robot), [{"type": "glance", "on": False, "id": robot.sent[0]["id"]}])
        self.assertIs(main.idle_sent, False)
        self.assertTrue(main.pose_synced)

    def test_no_round_trip_once_idle_is_off_and_synced(self):
        robot = FakeRobot()
        main.robot_socket = robot

        async def run():
            for _ in range(3):
                async with main.claim_head():
                    pass

        asyncio.run(run())
        self.assertEqual(len(self.glances(robot)), 1)

    def test_a_stale_ack_id_is_ignored(self):
        robot = FakeRobot(answer=False)
        main.robot_socket = robot

        async def run():
            async def wrong_then_right():
                await asyncio.sleep(0)
                rid = robot.sent[0]["id"]
                main._resolve_ack({"type": "idle_ack", "id": rid - 1, "pan": 99, "tilt": 99})
                main._resolve_ack({"type": "idle_ack", "id": rid, "pan": 3, "tilt": 4})
            helper = asyncio.create_task(wrong_then_right())
            async with main.claim_head():
                result = (main.tracker.pan, main.tracker.tilt)
            await helper
            return result

        self.assertEqual(asyncio.run(run()), (3.0, 4.0))

    def test_timeout_falls_back_and_does_not_stall_every_move(self):
        robot = FakeRobot(answer=False)
        main.robot_socket = robot
        main.tracker.note_pose(pan=5, tilt=6)

        async def run():
            with patch.object(config, "IDLE_ACK_SECONDS", 0.01):
                async with main.claim_head():
                    first = (main.tracker.pan, main.tracker.tilt)
                async with main.claim_head():
                    pass
            return first

        self.assertEqual(asyncio.run(run()), (5.0, 6.0))
        self.assertEqual(len(self.glances(robot)), 1)  # retry waits out the back-off
        self.assertIs(main.idle_sent, False)
        self.assertFalse(main.pose_synced)

    def test_disconnect_during_a_claim_releases_it(self):
        robot = FakeRobot(answer=False)
        main.robot_socket = robot

        async def run():
            async def drop():
                await asyncio.sleep(0)
                main.robot_socket = None
                main._reset_head_ownership()
            helper = asyncio.create_task(drop())
            async with main.claim_head():
                pass
            await helper

        asyncio.run(run())
        self.assertIsNone(main.idle_sent)
        self.assertFalse(main.pose_synced)
        self.assertEqual(main._ack_waiters, {})

    def test_reconnect_claims_again(self):
        robot = FakeRobot()
        main.robot_socket = robot

        async def run():
            async with main.claim_head():
                pass
            main._reset_head_ownership()  # a reconnect
            async with main.claim_head():
                pass

        asyncio.run(run())
        self.assertEqual(len(self.glances(robot)), 2)

    def test_heartbeat_with_serial_idle_forces_a_new_claim(self):
        main.idle_sent = False
        main.pose_synced = True
        main._note_heartbeat({"type": "state", "idle": False, "pan": 0, "tilt": 0})
        self.assertTrue(main.pose_synced)
        main._note_heartbeat({"type": "state", "idle": True, "pan": 0, "tilt": 0})
        self.assertIsNone(main.idle_sent)
        self.assertFalse(main.pose_synced)

    def test_look_reports_from_the_synced_pose(self):
        robot = FakeRobot(pose=(-20.0, 10.0))
        main.robot_socket = robot
        main.idle_sent = True
        main.eyes.source, saved_source = "robot", main.eyes.source

        async def no_wait(_seen):
            return None

        async def run():
            with patch.object(main, "_wait_for_look_frame", no_wait):
                return await main.look({"direction": "down"})

        try:
            text, _image = asyncio.run(run())
        finally:
            main.eyes.source = saved_source
        self.assertIn("pan -20 deg", text)
        moves = [m for m in robot.sent if m.get("type") in ("pan", "tilt")]
        self.assertEqual(moves, [{"type": "tilt", "deg": -30.0}])
        self.assertEqual(robot.sent[0]["type"], "glance")  # the claim went first


class HeadLoopTests(HeadOwnershipBase):
    def test_a_move_from_before_the_claim_is_dropped(self):
        robot = FakeRobot(pose=(30.0, 0.0))
        main.robot_socket = robot
        main.idle_sent = True
        stale_gen = main.tracker.pose_gen

        async def run():
            queue = asyncio.Queue()
            with patch.object(main, "head_moves", queue):
                task = asyncio.create_task(main.head_loop())
                queue.put_nowait((1.0, 2.0, True, stale_gen))
                for _ in range(20):
                    await asyncio.sleep(0)
                fresh = main.tracker.pose_gen
                queue.put_nowait((31.0, 0.0, True, fresh))
                for _ in range(20):
                    await asyncio.sleep(0)
                task.cancel()

        asyncio.run(run())
        moves = [m for m in robot.sent if m.get("type") in ("pan", "tilt")]
        self.assertEqual(moves, [{"type": "pan", "deg": 31.0}, {"type": "tilt", "deg": 0.0}])


class AwakeClockAndTrackingGuardTests(unittest.TestCase):
    """Idle follows staying_up() (the 60 s speech clock), not 'eyes open'."""

    def test_idle_stays_off_until_the_awake_clock_is_running(self):
        ctl = IdleController(8.0)
        # After connect, eyes can be open while awake_until is still 0.
        self.assertFalse(ctl.wanted(0.0, quiet(awake=False)))
        self.assertEqual(ctl.reason, "asleep")
        self.assertFalse(ctl.wanted(20.0, quiet(awake=False)))


class ResyncOscillationTests(HeadOwnershipBase):
    def test_once_idle_is_off_tracking_must_not_glance_again(self):
        robot = FakeRobot(pose=(25.0, 0.0))
        main.robot_socket = robot
        main.idle_sent = False
        main.pose_synced = False
        main.tracker.note_pose(pan=8.0, tilt=0.0)

        async def run():
            async with main.claim_head():
                return main.tracker.pan

        self.assertEqual(asyncio.run(run()), 8.0)
        self.assertEqual(self.glances(robot), [])
        self.assertFalse(main.pose_synced)

    def test_failed_ack_does_not_mark_the_pose_synced(self):
        robot = FakeRobot(answer=False)
        main.robot_socket = robot
        main.idle_sent = True

        async def run():
            with patch.object(config, "IDLE_ACK_SECONDS", 0.01):
                async with main.claim_head():
                    pass

        asyncio.run(run())
        self.assertIs(main.idle_sent, False)
        self.assertFalse(main.pose_synced)

    def test_steady_tracking_sends_without_a_glance(self):
        robot = FakeRobot(pose=(25.0, 0.0))
        main.robot_socket = robot
        main.idle_sent = False
        main.pose_synced = True
        gen = main.tracker.pose_gen

        async def run():
            queue = asyncio.Queue()
            with patch.object(main, "head_moves", queue):
                task = asyncio.create_task(main.head_loop())
                queue.put_nowait((12.0, -3.0, True, gen))
                queue.put_nowait((13.0, -3.0, True, gen))
                for _ in range(30):
                    await asyncio.sleep(0)
                task.cancel()

        asyncio.run(run())
        self.assertEqual(self.glances(robot), [])
        moves = [m for m in robot.sent if m.get("type") in ("pan", "tilt")]
        self.assertEqual(moves, [
            {"type": "pan", "deg": 12.0}, {"type": "tilt", "deg": -3.0},
            {"type": "pan", "deg": 13.0}, {"type": "tilt", "deg": -3.0},
        ])

    def test_tracking_start_claims_once_then_sends_direct(self):
        robot = FakeRobot(pose=(8.0, 0.0))
        main.robot_socket = robot
        main.idle_sent = True
        stale = main.tracker.pose_gen

        async def run():
            queue = asyncio.Queue()
            with patch.object(main, "head_moves", queue):
                task = asyncio.create_task(main.head_loop())
                queue.put_nowait((1.0, 0.0, True, stale))
                for _ in range(30):
                    await asyncio.sleep(0)
                queue.put_nowait((9.0, 0.0, True, main.tracker.pose_gen))
                for _ in range(30):
                    await asyncio.sleep(0)
                task.cancel()

        asyncio.run(run())
        self.assertEqual(len(self.glances(robot)), 1)
        moves = [m for m in robot.sent if m.get("type") in ("pan", "tilt")]
        self.assertEqual(moves, [{"type": "pan", "deg": 9.0}, {"type": "tilt", "deg": 0.0}])

    def test_set_tracking_owns_the_head_before_a_face(self):
        robot = FakeRobot(pose=(4.0, 1.0))
        main.robot_socket = robot
        main.idle_sent = True

        asyncio.run(main.set_tracking(True, announce=False))
        self.assertTrue(main.tracker.enabled)
        self.assertIs(main.idle_sent, False)
        self.assertEqual(len(self.glances(robot)), 1)
        self.assertEqual((main.tracker.pan, main.tracker.tilt), (4.0, 1.0))


class IdleLoopTests(HeadOwnershipBase):
    def test_on_after_quiet_then_off_when_hearing(self):
        robot = FakeRobot()
        main.robot_socket = robot
        main.idle_sent = False
        main.pose_synced = True
        clock = {"t": 0.0}
        state = {"hearing": False}
        ticks = {"n": 0}
        real_sleep = asyncio.sleep

        async def fake_sleep(_s):
            ticks["n"] += 1
            clock["t"] += 1.0
            if ticks["n"] == 12:
                state["hearing"] = True
            if ticks["n"] > 14:
                raise asyncio.CancelledError()
            await real_sleep(0)

        def inputs(now):
            return quiet(hearing=state["hearing"])

        async def run():
            with (
                patch("brain.main.asyncio.sleep", fake_sleep),
                patch("brain.main.time.monotonic", lambda: clock["t"]),
                patch.object(main, "idle_inputs", inputs),
                patch.object(config, "IDLE_ENABLED", True),
            ):
                await main.idle_loop()

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run())
        glances = [(m["on"], "id" in m) for m in self.glances(robot)]
        self.assertEqual(glances, [(True, False), (False, True)])

    def test_disabled_never_turns_idle_on(self):
        robot = FakeRobot()
        main.robot_socket = robot
        main.idle_sent = False
        main.pose_synced = True
        clock = {"t": 0.0}
        ticks = {"n": 0}

        async def fake_sleep(_s):
            ticks["n"] += 1
            clock["t"] += 5.0
            if ticks["n"] > 10:
                raise asyncio.CancelledError()

        async def run():
            with (
                patch("brain.main.asyncio.sleep", fake_sleep),
                patch("brain.main.time.monotonic", lambda: clock["t"]),
                patch.object(main, "idle_inputs", lambda now: quiet()),
                patch.object(config, "IDLE_ENABLED", False),
            ):
                await main.idle_loop()

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run())
        self.assertEqual(self.glances(robot), [])

    def test_idle_does_not_touch_the_awake_clock(self):
        saved = main.awake_until
        main.awake_until = 1234.0
        try:
            self.test_on_after_quiet_then_off_when_hearing()
            self.assertEqual(main.awake_until, 1234.0)
        finally:
            main.awake_until = saved


class StyleRobot:
    """Speaks the idle_style_ack the firmware would, without moving a head."""

    def __init__(self, firmware=None, reject_field=None, answer=True):
        self.sent = []
        self.firmware = {"preset": "curious", **config.IDLE_PRESETS["curious"]}
        if firmware:
            self.firmware.update(firmware)
        self.reject_field = reject_field
        self.answer = answer

    async def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if not self.answer or msg.get("type") != "idle_style":
            return
        event = {"type": "idle_style_ack", "id": msg.get("id"), "ok": True, **self.firmware}
        if self.reject_field and self.reject_field in msg and not msg.get("read"):
            event["ok"] = False
            event["error"] = self.reject_field
        elif msg.get("read"):
            pass
        elif "preset" in msg and "scale" in msg:
            self.firmware = {
                "preset": msg["preset"], "scale": msg["scale"], "duration": msg["duration"],
                "pause_min": msg["pause_min"], "pause_max": msg["pause_max"],
            }
            event = {"type": "idle_style_ack", "id": msg.get("id"), "ok": True, **self.firmware}
        elif "preset" in msg:
            self.firmware = {"preset": msg["preset"], **config.IDLE_PRESETS[msg["preset"]]}
            event = {"type": "idle_style_ack", "id": msg.get("id"), "ok": True, **self.firmware}
        elif "scale" in msg:
            self.firmware["scale"] = msg["scale"]
            event.update(self.firmware)
        elif "duration" in msg:
            self.firmware["duration"] = msg["duration"]
            event.update(self.firmware)
        elif "pause_min" in msg:
            self.firmware["pause_min"] = msg["pause_min"]
            self.firmware["pause_max"] = msg["pause_max"]
            event.update(self.firmware)
        asyncio.get_running_loop().call_soon(main._resolve_style_ack, event)


class IdleStyleTests(HeadOwnershipBase):
    """Personalities are configured, not a second motion engine."""

    def setUp(self):
        super().setUp()
        self._saved_style = (
            main.idle_personality,
            {name: dict(knobs) for name, knobs in main.idle_personalities.items()},
            main.idle_confirmed,
        )
        main.idle_personalities = main._fresh_personalities()
        main.idle_personality = "curious"
        main.idle_confirmed = False
        main.pose_synced = True
        main.idle_sent = False

    def tearDown(self):
        main.idle_personality, personalities, main.idle_confirmed = self._saved_style
        main.idle_personalities = personalities
        main._style_waiters.clear()
        super().tearDown()

    def test_curious_starts_at_the_preferred_desk_settings(self):
        self.assertEqual(config.IDLE_PRESETS["curious"], {
            "scale": 1.2, "duration": 0.85, "pause_min": 2500, "pause_max": 5500,
        })
        self.assertEqual(config.IDLE_PRESETS["calm"], {
            "scale": 0.70, "duration": 1.30, "pause_min": 3500, "pause_max": 8000,
        })
        self.assertEqual(config.IDLE_PRESETS["lively"], {
            "scale": 1.10, "duration": 0.80, "pause_min": 1200, "pause_max": 3500,
        })

    def test_selecting_a_default_personality_sends_only_the_preset(self):
        robot = StyleRobot()
        main.robot_socket = robot
        gen = main.tracker.pose_gen
        asyncio.run(main._apply_idle_style({"name": "lively"}))
        sent = robot.sent[0]
        self.assertEqual(sent["type"], "idle_style")
        self.assertEqual(sent["preset"], "lively")
        self.assertNotIn("scale", sent)
        self.assertEqual(main.idle_personality, "lively")
        self.assertTrue(main.idle_confirmed)
        self.assertEqual(main.idle_personalities["lively"]["pause_min"], 1200)
        self.assertIs(main.idle_sent, False)
        self.assertTrue(main.pose_synced)
        self.assertEqual(main.tracker.pose_gen, gen)
        self.assertFalse(any(m["type"] in ("glance", "pan", "tilt") for m in robot.sent))

    def test_each_personality_keeps_its_own_knobs(self):
        robot = StyleRobot()
        main.robot_socket = robot
        asyncio.run(main._apply_idle_style({"scale": 0.5}))
        self.assertEqual(robot.sent[0]["scale"], 0.5)
        self.assertNotIn("preset", robot.sent[0])
        self.assertEqual(main.idle_personalities["curious"]["scale"], 0.5)
        self.assertEqual(main.idle_personalities["calm"]["scale"], 0.70)
        self.assertEqual(main.idle_personalities["lively"]["scale"], 1.10)
        asyncio.run(main._apply_idle_style({"name": "calm"}))
        self.assertEqual(robot.sent[-1]["preset"], "calm")
        self.assertNotIn("scale", robot.sent[-1])
        self.assertEqual(main.idle_personalities["curious"]["scale"], 0.5)
        asyncio.run(main._apply_idle_style({"name": "curious"}))
        restored = robot.sent[-1]
        self.assertEqual(restored["preset"], "curious")
        self.assertEqual(restored["scale"], 0.5)
        self.assertEqual(restored["pause_min"], 2500)
        self.assertEqual(main.idle_personalities["calm"]["pause_max"], 8000)

    def test_reset_restores_only_the_selected_personality(self):
        robot = StyleRobot()
        main.robot_socket = robot
        main.idle_personalities["lively"]["scale"] = 1.4
        asyncio.run(main._apply_idle_style({"scale": 0.5}))
        asyncio.run(main._apply_idle_style({"reset": True}))
        reset = robot.sent[-1]
        self.assertEqual(reset["preset"], "curious")
        self.assertNotIn("scale", reset)
        self.assertEqual(main.idle_personalities["curious"]["scale"], 1.2)
        self.assertEqual(main.idle_personalities["lively"]["scale"], 1.4)
        self.assertFalse(main.console_state()["idle"]["adjusted"])

    def test_pause_minimum_above_maximum_is_rejected_before_send(self):
        robot = StyleRobot()
        main.robot_socket = robot
        with self.assertRaises(ValueError):
            asyncio.run(main._apply_idle_style({"pause_min": 4000, "pause_max": 1000}))
        self.assertEqual(robot.sent, [])
        self.assertFalse(main.idle_confirmed)

    def test_scale_outside_the_calibrator_range_is_rejected_before_send(self):
        robot = StyleRobot()
        main.robot_socket = robot
        for bad in (0.29, 1.51, 2):
            with self.assertRaises(ValueError):
                asyncio.run(main._apply_idle_style({"scale": bad}))
        with self.assertRaises(ValueError):
            asyncio.run(main._apply_idle_style({"duration": 0.39}))
        with self.assertRaises(ValueError):
            asyncio.run(main._apply_idle_style({"pause_min": 199, "pause_max": 1000}))
        self.assertEqual(robot.sent, [])

    def test_a_rejected_ack_does_not_keep_the_requested_value(self):
        robot = StyleRobot(reject_field="scale")
        main.robot_socket = robot
        asyncio.run(main._read_idle_style())
        with self.assertRaises(ValueError):
            asyncio.run(main._apply_idle_style({"scale": 0.9}))
        self.assertEqual(main.idle_personalities["curious"]["scale"], 1.2)
        self.assertTrue(main.idle_confirmed)
        self.assertEqual(main.console_state()["idle"]["scale"], 1.2)

    def test_claim_does_not_send_a_personality(self):
        robot = FakeRobot()
        main.robot_socket = robot
        main.idle_sent = None
        asyncio.run(main._claim_on_connect())
        self.assertEqual(robot.sent[0]["type"], "glance")
        self.assertFalse(any(m.get("type") == "idle_style" for m in robot.sent))
        self.assertIs(main.idle_sent, False)

    def test_reconnect_reads_the_firmware_and_leaves_other_personalities(self):
        robot = StyleRobot(firmware={"preset": "lively", **config.IDLE_PRESETS["lively"]})
        main.robot_socket = robot
        main.idle_personalities["curious"]["scale"] = 0.5
        main.idle_personality = "curious"
        asyncio.run(main._read_idle_style())
        self.assertEqual(set(robot.sent[0]), {"type", "id", "read"})
        self.assertTrue(robot.sent[0]["read"])
        self.assertEqual(main.idle_personality, "lively")
        self.assertTrue(main.idle_confirmed)
        self.assertEqual(main.idle_personalities["curious"]["scale"], 0.5)
        self.assertEqual(main.idle_personalities["lively"]["scale"], 1.10)
        self.assertIs(main.idle_sent, False)
        self.assertTrue(main.pose_synced)

    def test_a_missed_read_does_not_present_stale_knobs(self):
        robot = StyleRobot(answer=False)
        main.robot_socket = robot
        main.idle_personalities["curious"]["scale"] = 0.5
        with patch.object(config, "IDLE_ACK_SECONDS", 0.05):
            asyncio.run(main._read_idle_style())
        self.assertFalse(main.idle_confirmed)
        shown = main.console_state()["idle"]
        self.assertIsNone(shown["personality"])
        self.assertNotIn("scale", shown)

    def test_disconnect_hides_knobs_but_keeps_the_session(self):
        main.idle_confirmed = True
        main.idle_personalities["calm"]["scale"] = 0.4
        main._reset_head_ownership()
        self.assertFalse(main.idle_confirmed)
        self.assertEqual(main.idle_personalities["calm"]["scale"], 0.4)
        self.assertNotIn("scale", main.console_state()["idle"])


if __name__ == "__main__":
    unittest.main()
