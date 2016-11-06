import asyncio

from mpf.core.utility_functions import Util
from mpf.devices.ball_device.ball_device_state_handler import BallDeviceStateHandler


class EjectTracker:

    """Tracks ball changes during an eject."""

    def __init__(self, ball_counter_handler, already_left):
        self.machine = ball_counter_handler.machine
        self._already_left = already_left
        self._ball_count_handler = ball_counter_handler
        self._hardware_counter_state = None
        self._task = None
        self._event_queue = asyncio.Queue(loop=self._ball_count_handler.machine.clock.loop)
        self._eject_done = asyncio.Future(loop=self._ball_count_handler.machine.clock.loop)
        self._ball_left = asyncio.Future(loop=self._ball_count_handler.machine.clock.loop)
        self._ball_returned = asyncio.Future(loop=self._ball_count_handler.machine.clock.loop)
        self._ball_entrance_queue = asyncio.Queue(loop=self._ball_count_handler.machine.clock.loop)
        self._ready = asyncio.Future(loop=self._ball_count_handler.machine.clock.loop)
        self._unknown_balls = asyncio.Future(loop=self._ball_count_handler.machine.clock.loop)
        self._num_unknown_balls = 0

    @asyncio.coroutine
    def will_eject(self):
        """Start process."""
        self._task = self.machine.clock.loop.create_task(
            self._ball_count_handler.ball_device.counter.track_eject(self, self._already_left))
        self._task.add_done_callback(self._done)
        yield from self.wait_for_ready()

    def _done(self, future):
        try:
            future.result()
        except asyncio.CancelledError:
            pass

    def is_jammed(self):
        """Return true if currently jammed."""
        return self._ball_count_handler.ball_device.counter.is_jammed()

    def track_ball_left(self):
        self._ball_count_handler.ball_device.debug_log("Got ball left during eject")
        self._ball_left.set_result(True)

    def track_ball_returned(self):
        self._ball_count_handler.ball_device.debug_log("Got ball return during eject")
        self._ball_returned.set_result(True)

    def track_ball_entrance(self):
        self._ball_count_handler.ball_device.debug_log("Got ball entrance during eject")
        self._ball_entrance_queue.put_nowait(1)

    def track_unknown_balls(self, balls):
        self._ball_count_handler.ball_device.debug_log("Got %s unknown ball during eject", balls)
        self._num_unknown_balls += balls
        self._unknown_balls.set_result(True)

    def wait_for_ball_return(self):
        return asyncio.shield(self._ball_returned, loop=self.machine.clock.loop)

    def wait_for_ball_unknown_ball(self):
        """Return true if the device has unknown balls which are neither clearly new or returned."""
        return asyncio.shield(self._unknown_balls, loop=self.machine.clock.loop)

    def wait_for_ball_left(self):
        return asyncio.shield(self._ball_left, loop=self.machine.clock.loop)

    def wait_for_ready(self):
        return asyncio.shield(self._ready, loop=self.machine.clock.loop)

    def wait_for_ball_entrance(self):
        return self._ball_entrance_queue.get()

    def wait_for_eject_done(self):
        return asyncio.shield(self._eject_done, loop=self.machine.clock.loop)

    def set_ready(self):
        self._ready.set_result("ready")

    def eject_success(self):
        self._task.cancel()
        self._ball_count_handler.eject_success()

    def ball_lost(self):
        self._task.cancel()
        self._ball_count_handler.ball_lost()

    def ball_returned(self):
        self._task.cancel()
        self._ball_count_handler.ball_returned()


class BallCountHandler(BallDeviceStateHandler):

    """Handles the ball count in the device."""

    def __init__(self, ball_device):
        """Initialise ball count handler."""
        super().__init__(ball_device)
        # inputs
        self._is_counting = asyncio.Lock(loop=self.machine.clock.loop)
        self._eject_started = asyncio.Event(loop=self.machine.clock.loop)
        self._has_balls = asyncio.Event(loop=self.machine.clock.loop)
        self._ball_count = 0
        self._ball_count_changed_futures = []

    def wait_for_ball_count_changed(self):
        """Wait until ball count changed."""
        future = asyncio.Future(loop=self.machine.clock.loop)
        self._ball_count_changed_futures.append(future)
        return future

    @property
    def handled_balls(self):
        """Return balls which are already handled."""
        return self._ball_count

    @property
    def unhandled_balls(self):
        """Return balls which are already handled."""
        return self._ball_count

    @property
    def legacy_handled_balls(self):
        """Return balls which are already handled."""
        if self.ball_device.outgoing_balls_handler.state in ["ball_left", "failed_confirm"]:
            # TODO: remove this quirk for old tests
            return self._ball_count - 1
        return self._ball_count

    def _set_ball_count(self, count):
        self._ball_count = count
        if self._ball_count > 0:
            self._has_balls.set()
        else:
            self._has_balls.clear()

        for future in self._ball_count_changed_futures:
            if not future.done():
                future.set_result(count)

        # reset futures
        self._ball_count_changed_futures = []

    @asyncio.coroutine
    def initialise(self):
        """Initialise handler."""
        self._ball_count = yield from self.ball_device.counter.count_balls()
        if self._ball_count > 0:
            self._has_balls.set()
        yield from super().initialise()

    @asyncio.coroutine
    def wait_for_ball(self):
        """Wait until the device has a ball."""
        if self._ball_count > 0:
            self.debug_log("We have %s balls.", self._ball_count)
            return

        self.debug_log("No ball found. Waiting for balls.")

        # wait until we have more than 0 balls
        ball_changes = self.ball_device.ensure_future(self.ball_device.counter.wait_for_ball_count_changes(0))
        new_balls = yield from ball_changes

        # update count
        old_ball_count = self._ball_count
        self._ball_count = new_balls
        if new_balls > old_ball_count:
            self.debug_log("BCH: Found %s new balls", new_balls - old_ball_count)
            # handle new balls via incoming balls handler
            for _ in range(new_balls - old_ball_count):
                yield from self.ball_device.incoming_balls_handler.ball_arrived()
            self._set_ball_count(new_balls)

        self.debug_log("A ball arrived. Progressing.")

    def get_ball_count(self):
        """Return a ball count future."""
        return self.ball_device.counter.count_balls()   # TODO: internalise counter

    @asyncio.coroutine
    def wait_for_ready_to_receive(self, source):
        while True:
            free_space = self.ball_device.config['ball_capacity'] - self._ball_count
            incoming_balls = len(self.ball_device.incoming_balls_handler._incoming_balls)
            if free_space > incoming_balls:
                self.debug_log("Ready to receive from %s. Free space %s (Capacity: %s, Balls: %s), incoming_balls: %s",
                               source, free_space, self.ball_device.config['ball_capacity'], self._ball_count,
                               incoming_balls)
                return True

            self.debug_log("Not ready to receive from %s. Free space %s (Capacity: %s, Balls: %s), incoming_balls: %s",
                           source, free_space, self.ball_device.config['ball_capacity'], self._ball_count,
                           incoming_balls)

            yield from self.wait_for_ball_count_changed()

    @asyncio.coroutine
    def start_eject(self):
        yield from self._is_counting.acquire()
        self._eject_started.set()
        self.debug_log("Entered eject mode.")

    @asyncio.coroutine
    def end_eject(self):
        self.debug_log("Exited eject mode.")
        self._eject_started.clear()
        self._is_counting.release()

    @asyncio.coroutine
    def track_eject(self, already_left=False) -> EjectTracker:
        """Start an eject."""
        eject_process = EjectTracker(self, already_left)
        if already_left:
            self._set_ball_count(self._ball_count + 1)
            yield from eject_process.will_eject()
        return eject_process

    @asyncio.coroutine
    def _run(self):
        while True:
            # wait for ball changes
            ball_changes = self.ball_device.ensure_future(
                self.ball_device.counter.wait_for_ball_count_changes(self._ball_count))
            event = yield from Util.first([ball_changes, self._eject_started.wait()], loop=self.machine.clock.loop)

            # get lock and update count
            if self._is_counting.locked():
                self.debug_log("Waiting for eject to end")
                yield from self._is_counting.acquire()
                new_balls = yield from self.ball_device.counter.count_balls()
                self.debug_log("Eject ended")
            else:
                if event != ball_changes:
                    raise AssertionError("Event order problem")
                new_balls = yield from ball_changes
                yield from self._is_counting.acquire()

            self.debug_log("Counting idle")

            old_ball_count = self._ball_count
            self._set_ball_count(new_balls)
            if new_balls > old_ball_count:
                self.debug_log("BCH: Found %s new balls", new_balls - old_ball_count)
                # handle new balls via incoming balls handler
                for _ in range(new_balls - old_ball_count):
                    yield from self.ball_device.incoming_balls_handler.ball_arrived()
            elif new_balls < old_ball_count:
                self.debug_log("BCH: Lost %s balls", old_ball_count - new_balls)
                for _ in range(old_ball_count - new_balls):
                    yield from self.ball_device.lost_idle_ball()

            self._is_counting.release()

    @asyncio.coroutine
    def _handle_entrance_during_eject(self, eject_process: EjectTracker):
        """Wait until the eject is done and handle limited ball entrance."""
        self.debug_log("Counting in eject mode")
        # if we are 100% certain that this ball entered and did not return
        ball_entrance = self.ball_device.ensure_future(eject_process.wait_for_ball_entrance())
        eject_done = self.ball_device.ensure_future(eject_process.wait_for_eject_done())
        ball_changes = self.ball_device.ensure_future(self.ball_device.counter.wait_for_ball_activity())
        while True:
            futures = [eject_done, ball_entrance, ball_changes]
            event = yield from Util.first(futures, loop=self.machine.clock.loop, cancel_others=False)

            if eject_done.done():
                ball_entrance.cancel()
                ball_changes.cancel()
                result = yield from eject_done
                self.debug_log("XXX Eject done. Result: %s", result)
                yield from self._handle_eject_done(result)
                return

            if ball_changes.done():
                ball_changes = self.ball_device.ensure_future(self.ball_device.counter.wait_for_ball_activity())
                # yield from self._updated_balls(True)

            if ball_entrance.done():
                # TODO: handle new ball via incoming balls handler
                ball_entrance = self.ball_device.ensure_future(eject_process.wait_for_ball_entrance())

    def _handle_eject_done(self, result):
        """Decrement count by one and handle failures."""
        if result == "success":
            self.debug_log("Received eject done.")
            self._set_ball_count(self._ball_count - 1)
        elif result == "returned":
            self.debug_log("Received eject failed. Ball returned.")
        elif result == "lost":
            self.ball_device.log.warning("Received eject failed. Eject lost ball.")
            self._set_ball_count(self._ball_count - 1)
            # handle lost balls via lost balls handler
        else:
            raise AssertionError("invalid result %s", result)

    def eject_success(self):
        self.debug_log("Received eject success.")
        self._set_ball_count(self._ball_count - 1)

    def ball_lost(self):
        self.ball_device.log.warning("Received eject failed. Eject lost ball.")
        self._set_ball_count(self._ball_count - 1)

    def ball_returned(self):
        self.debug_log("Received eject failed. Ball returned.")
