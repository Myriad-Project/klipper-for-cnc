# Code for coordinating events on the printer toolhead
#
# Copyright (C) 2016-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging, importlib
import mcu, chelper, kinematics.extruder
import time

# Common suffixes: _d is distance (in mm), _v is velocity (in
#   mm/second), _v2 is velocity squared (mm^2/s^2), _t is time (in
#   seconds), _r is ratio (scalar between 0.0 and 1.0)

# Class to track each move request
class Move:
    def __init__(self, toolhead, start_pos, end_pos, speed):
        self.toolhead = toolhead
        self.start_pos = tuple(start_pos)
        self.end_pos = tuple(end_pos)
        self.accel = toolhead.max_accel
        self.junction_deviation = toolhead.junction_deviation
        self.timing_callbacks = []
        velocity = min(speed, toolhead.max_velocity)
        self.is_kinematic_move = True

        # NOTE: compute the 4 components of the displacement vector.
        self.axes_d = axes_d = [end_pos[i] - start_pos[i] for i in (0, 1, 2, 3)]
        
        # NOTE: compute the euclidean magnitude of the displacement vector.
        self.move_d = move_d = math.sqrt(sum([d*d for d in axes_d[:3]]))
        
        # TODO: this seems strange, some numerical instability handling, probably.
        if move_d < .000000001:
            # Extrude only move
            self.end_pos = (start_pos[0], start_pos[1], start_pos[2],
                            end_pos[3])
            axes_d[0] = axes_d[1] = axes_d[2] = 0.
            self.move_d = move_d = abs(axes_d[3])
            inv_move_d = 0.
            if move_d:
                inv_move_d = 1. / move_d
            self.accel = 99999999.9
            velocity = speed
            self.is_kinematic_move = False
        else:
            inv_move_d = 1. / move_d
        
        # NOTE: compute a ratio between each component of the displacement
        #       vector and the total magnitude.
        self.axes_r = [d * inv_move_d for d in axes_d]
        
        self.min_move_t = move_d / velocity
        # Junction speeds are tracked in velocity squared.  The
        # delta_v2 is the maximum amount of this squared-velocity that
        # can change in this move.
        self.max_start_v2 = 0.
        self.max_cruise_v2 = velocity**2
        self.delta_v2 = 2.0 * move_d * self.accel
        self.max_smoothed_v2 = 0.
        self.smooth_delta_v2 = 2.0 * move_d * toolhead.max_accel_to_decel
    def limit_speed(self, speed, accel):
        speed2 = speed**2
        if speed2 < self.max_cruise_v2:
            self.max_cruise_v2 = speed2
            self.min_move_t = self.move_d / speed
        self.accel = min(self.accel, accel)
        self.delta_v2 = 2.0 * self.move_d * self.accel
        self.smooth_delta_v2 = min(self.smooth_delta_v2, self.delta_v2)
    def move_error(self, msg="Move out of range"):
        ep = self.end_pos
        m = "%s: %.3f %.3f %.3f [%.3f]" % (msg, ep[0], ep[1], ep[2], ep[3])
        return self.toolhead.printer.command_error(m)
    def calc_junction(self, prev_move):
        if not self.is_kinematic_move or not prev_move.is_kinematic_move:
            return
        # Allow extruder to calculate its maximum junction
        extruder_v2 = self.toolhead.extruder.calc_junction(prev_move, self)
        # Find max velocity using "approximated centripetal velocity"
        axes_r = self.axes_r
        prev_axes_r = prev_move.axes_r
        junction_cos_theta = -(axes_r[0] * prev_axes_r[0]
                               + axes_r[1] * prev_axes_r[1]
                               + axes_r[2] * prev_axes_r[2])
        if junction_cos_theta > 0.999999:
            return
        junction_cos_theta = max(junction_cos_theta, -0.999999)
        sin_theta_d2 = math.sqrt(0.5*(1.0-junction_cos_theta))
        R_jd = sin_theta_d2 / (1. - sin_theta_d2)
        # Approximated circle must contact moves no further away than mid-move
        tan_theta_d2 = sin_theta_d2 / math.sqrt(0.5*(1.0+junction_cos_theta))
        move_centripetal_v2 = .5 * self.move_d * tan_theta_d2 * self.accel
        prev_move_centripetal_v2 = (.5 * prev_move.move_d * tan_theta_d2
                                    * prev_move.accel)
        # Apply limits
        self.max_start_v2 = min(
            R_jd * self.junction_deviation * self.accel,
            R_jd * prev_move.junction_deviation * prev_move.accel,
            move_centripetal_v2, prev_move_centripetal_v2,
            extruder_v2, self.max_cruise_v2, prev_move.max_cruise_v2,
            prev_move.max_start_v2 + prev_move.delta_v2)
        self.max_smoothed_v2 = min(
            self.max_start_v2
            , prev_move.max_smoothed_v2 + prev_move.smooth_delta_v2)
    def set_junction(self, start_v2, cruise_v2, end_v2):
        # Determine accel, cruise, and decel portions of the move distance
        half_inv_accel = .5 / self.accel
        accel_d = (cruise_v2 - start_v2) * half_inv_accel
        decel_d = (cruise_v2 - end_v2) * half_inv_accel
        cruise_d = self.move_d - accel_d - decel_d
        # Determine move velocities
        self.start_v = start_v = math.sqrt(start_v2)
        self.cruise_v = cruise_v = math.sqrt(cruise_v2)
        self.end_v = end_v = math.sqrt(end_v2)
        # Determine time spent in each portion of move (time is the
        # distance divided by average velocity)
        self.accel_t = accel_d / ((start_v + cruise_v) * 0.5)
        self.cruise_t = cruise_d / cruise_v
        self.decel_t = decel_d / ((end_v + cruise_v) * 0.5)

LOOKAHEAD_FLUSH_TIME = 0.250

# Class to track a list of pending move requests and to facilitate
# "look-ahead" across moves to reduce acceleration between moves.
class MoveQueue:
    def __init__(self, toolhead):
        self.toolhead = toolhead
        self.queue = []
        self.junction_flush = LOOKAHEAD_FLUSH_TIME
    def reset(self):
        del self.queue[:]
        self.junction_flush = LOOKAHEAD_FLUSH_TIME
    def set_flush_time(self, flush_time):
        self.junction_flush = flush_time
    def get_last(self):
        if self.queue:
            return self.queue[-1]
        return None
    def flush(self, lazy=False):
        # NOTE: called by "add_move" when: 
        #       "Enough moves have been queued to reach the target flush time."
        #       Also called by "flush_step_generation".
        
        self.junction_flush = LOOKAHEAD_FLUSH_TIME
        
        # NOTE: logging for tracing activity
        logging.info("\n\nMoveQueue flush: function triggered.\n\n")
        
        update_flush_count = lazy
        queue = self.queue
        flush_count = len(queue)
        # Traverse queue from last to first move and determine maximum
        # junction speed assuming the robot comes to a complete stop
        # after the last move.
        delayed = []
        next_end_v2 = next_smoothed_v2 = peak_cruise_v2 = 0.
        for i in range(flush_count-1, -1, -1):
            move = queue[i]
            reachable_start_v2 = next_end_v2 + move.delta_v2
            start_v2 = min(move.max_start_v2, reachable_start_v2)
            reachable_smoothed_v2 = next_smoothed_v2 + move.smooth_delta_v2
            smoothed_v2 = min(move.max_smoothed_v2, reachable_smoothed_v2)
            if smoothed_v2 < reachable_smoothed_v2:
                # It's possible for this move to accelerate
                if (smoothed_v2 + move.smooth_delta_v2 > next_smoothed_v2
                    or delayed):
                    # This move can decelerate or this is a full accel
                    # move after a full decel move
                    if update_flush_count and peak_cruise_v2:
                        flush_count = i
                        update_flush_count = False
                    peak_cruise_v2 = min(move.max_cruise_v2, (
                        smoothed_v2 + reachable_smoothed_v2) * .5)
                    if delayed:
                        # Propagate peak_cruise_v2 to any delayed moves
                        if not update_flush_count and i < flush_count:
                            mc_v2 = peak_cruise_v2
                            for m, ms_v2, me_v2 in reversed(delayed):
                                mc_v2 = min(mc_v2, ms_v2)
                                m.set_junction(min(ms_v2, mc_v2), mc_v2
                                               , min(me_v2, mc_v2))
                        del delayed[:]
                if not update_flush_count and i < flush_count:
                    cruise_v2 = min((start_v2 + reachable_start_v2) * .5
                                    , move.max_cruise_v2, peak_cruise_v2)
                    move.set_junction(min(start_v2, cruise_v2), cruise_v2
                                      , min(next_end_v2, cruise_v2))
            else:
                # Delay calculating this move until peak_cruise_v2 is known
                delayed.append((move, start_v2, next_end_v2))
            next_end_v2 = start_v2
            next_smoothed_v2 = smoothed_v2
        
        if update_flush_count or not flush_count:
            return
        
        # Generate step times for all moves ready to be flushed
        # NOTE: So far, the clock time when this move will be sent are not known.
        self.toolhead._process_moves(moves=queue[:flush_count])

        # Remove processed moves from the queue
        del queue[:flush_count]

    def add_move(self, move):
        self.queue.append(move)
        if len(self.queue) == 1:
            return
        move.calc_junction(self.queue[-2])
        self.junction_flush -= move.min_move_t
        if self.junction_flush <= 0.:
            # Enough moves have been queued to reach the target flush time.
            self.flush(lazy=True)

# TODO: this quantity is undocumented.
MIN_KIN_TIME = 0.100

# NOTE: Some insight on this parameter may be available here:
#       https://github.com/Klipper3d/klipper/commit/7ca86f17232e5e0653de512b6322c301b153919c
MOVE_BATCH_TIME = 0.500

SDS_CHECK_TIME = 0.001 # step+dir+step filter in stepcompress.c

DRIP_SEGMENT_TIME = 0.050
DRIP_TIME = 0.100
class DripModeEndSignal(Exception):
    pass

# Main code to track events (and their timing) on the printer toolhead
class ToolHead:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.all_mcus = [
            m for n, m in self.printer.lookup_objects(module='mcu')]
        self.mcu = self.all_mcus[0]
        self.can_pause = True
        if self.mcu.is_fileoutput():
            self.can_pause = False
        self.move_queue = MoveQueue(self)
        self.commanded_pos = [0., 0., 0., 0.]
        self.printer.register_event_handler("klippy:shutdown",
                                            self._handle_shutdown)
        # Velocity and acceleration control
        self.max_velocity = config.getfloat('max_velocity', above=0.)
        self.max_accel = config.getfloat('max_accel', above=0.)
        self.requested_accel_to_decel = config.getfloat(
            'max_accel_to_decel', self.max_accel * 0.5, above=0.)
        self.max_accel_to_decel = self.requested_accel_to_decel
        self.square_corner_velocity = config.getfloat(
            'square_corner_velocity', 5., minval=0.)
        self.junction_deviation = 0.
        self._calc_junction_deviation()
        # Print time tracking
        self.buffer_time_low = config.getfloat(
            'buffer_time_low', 1.000, above=0.)
        self.buffer_time_high = config.getfloat(
            'buffer_time_high', 2.000, above=self.buffer_time_low)
        self.buffer_time_start = config.getfloat(
            'buffer_time_start', 0.250, above=0.)
        self.move_flush_time = config.getfloat(
            'move_flush_time', 0.050, above=0.)
        self.print_time = 0.
        self.special_queuing_state = "Flushed"
        self.need_check_stall = -1.
        self.flush_timer = self.reactor.register_timer(self._flush_handler)
        self.move_queue.set_flush_time(self.buffer_time_high)
        self.idle_flush_print_time = 0.
        self.print_stall = 0
        self.drip_completion = None
        # Kinematic step generation scan window time tracking
        self.kin_flush_delay = SDS_CHECK_TIME
        self.kin_flush_times = []
        self.last_kin_flush_time = self.last_kin_move_time = 0.
        # Setup iterative solver
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.step_generators = []
        # Create kinematics class
        gcode = self.printer.lookup_object('gcode')
        self.Coord = gcode.Coord
        self.extruder = kinematics.extruder.DummyExtruder(self.printer)
        kin_name = config.get('kinematics')
        try:
            mod = importlib.import_module('kinematics.' + kin_name)
            self.kin = mod.load_kinematics(self, config)
        except config.error as e:
            raise
        except self.printer.lookup_object('pins').error as e:
            raise
        except:
            msg = "Error loading kinematics '%s'" % (kin_name,)
            logging.exception(msg)
            raise config.error(msg)
        # Register commands
        gcode.register_command('G4', self.cmd_G4)
        gcode.register_command('M400', self.cmd_M400)
        gcode.register_command('SET_VELOCITY_LIMIT',
                               self.cmd_SET_VELOCITY_LIMIT,
                               desc=self.cmd_SET_VELOCITY_LIMIT_help)
        gcode.register_command('M204', self.cmd_M204)
        # Load some default modules
        modules = ["gcode_move", "homing", "idle_timeout", "statistics",
                   "manual_probe", "tuning_tower"]
        for module_name in modules:
            self.printer.load_object(config, module_name)
    # Print time tracking
    def _update_move_time(self, next_print_time):
        batch_time = MOVE_BATCH_TIME
        # NOTE: called by "flush_step_generation", "_process_moves", 
        #       "dwell", and "_update_drip_move_time".
        # NOTE: This function updates "self.print_time" directly.
        #       It updates "self.print_time" until it is greater than
        #       the provided "next_print_time".
        # NOTE: It also calls trapq_finalize_moves on the extruder and toolhead.
        # NOTE: a possible "use case" in the code is to:
        #           "Generate steps for moves"

        kin_flush_delay = self.kin_flush_delay
        lkft = self.last_kin_flush_time
        # TODO: I don't yet understand what the loop is meant to accomplish.
        while 1:
            self.print_time = min(self.print_time + batch_time, next_print_time)
            sg_flush_time = max(lkft, self.print_time - kin_flush_delay)
            for sg in self.step_generators:
                # NOTE: this list has been populated with "generate_steps" functions,
                #       one per stepper. Those in turn end up calling "ffi_lib.itersolve_generate_steps"
                #       which it meant to "Generate step times for a range of moves on the trapq".
                sg(sg_flush_time)
            free_time = max(lkft, sg_flush_time - kin_flush_delay)
            
            # NOTE: Update move times on the toolhead, meaning:
            #           "Expire any moves older than `free_time` from
            #           the trapezoid velocity queue" (see trapq.c).
            self.trapq_finalize_moves(self.trapq, free_time)
            
            # NOTE: Update move times on the extruder
            #       by calling "trapq_finalize_moves" in PrinterExtruder.
            self.extruder.update_move_time(free_time)

            mcu_flush_time = max(lkft, sg_flush_time - self.move_flush_time)
            for m in self.all_mcus:
                # NOTE: The following may find and transmit any scheduled steps 
                #       prior to the given 'mcu_flush_time' (see stepcompress.c).
                m.flush_moves(mcu_flush_time)
            if self.print_time >= next_print_time:
                break
    
    def _calc_print_time(self):
        # NOTE: called during "special" queuing states, 
        #       by "get_last_move_time" or "_process_moves".
        # NOTE: This function updates "self.print_time" directly.

        # NOTE: get the current (host) system time.
        curtime = self.reactor.monotonic()
        
        # NOTE: method from MCU (at mcu.py) that calls the
        #       "self._clocksync.estimated_print_time" 
        #       method from the ClockSync class (at clocksync.py).
        #       The method uses "get_clock" to get "self.clock_est" 
        #       from the ClockSync class. That object is updated in 
        #       the background by "_handle_clock" which:
        #       "is invoked from background thread" for "MCU clock querying".
        est_print_time = self.mcu.estimated_print_time(curtime)

        # NOTE: Guessing that the following adds potential delays to 
        #       the MCU time, estimating a "minimum print time".
        kin_time = max(est_print_time + MIN_KIN_TIME, self.last_kin_flush_time)
        kin_time += self.kin_flush_delay
        min_print_time = max(est_print_time + self.buffer_time_start, kin_time)

        if min_print_time > self.print_time:
            self.print_time = min_print_time
            self.printer.send_event("toolhead:sync_print_time",
                                    curtime, est_print_time, self.print_time)
    def _process_moves(self, moves):
        # NOTE: this ToolHead method is called during the execution of 
        #       the "flush" method in a "MoveQueue" class instance.
        #       The "moves" argument receives a "queue" of moves "ready to be flushed".
        
        # NOTE: logging for tracing activity
        logging.info("\n\nToolHead _process_moves: function triggered.\n\n")
        
        # Resync print_time if necessary
        if self.special_queuing_state:
            if self.special_queuing_state != "Drip":
                # Transition from "Flushed"/"Priming" state to main state
                self.special_queuing_state = ""
                self.need_check_stall = -1.
                # NOTE: updates the "self._next_timer" object in the "reactor".
                self.reactor.update_timer(self.flush_timer, self.reactor.NOW)
            
            # NOTE Update "self.print_time".
            self._calc_print_time()
            # NOTE: Also sends a "toolhead:sync_print_time" event, handled by
            #       "handle_sync_print_time" at "idle_timeout.py". It calls
            #       "reactor.update_timer" and sends an "idle_timeout:printing" 
            #       event (which is only handled by tmc2660.py).
            logging.info(f"\n\nToolHead _process_moves: self.print_time={str(self.print_time)}\n\n")
        
        # Queue moves into trapezoid motion queue (trapq)
        # NOTE: the "trapq" is possibly something like a CFFI object.
        #       From the following I interpret that it is actually this
        #       object the one responsible for sending commands to
        #       the MCUs.
        next_move_time = self.print_time
        for move in moves:
            logging.info(f"ToolHead _process_moves: next_move_time={str(next_move_time)}")
            if move.is_kinematic_move:
                self.trapq_append(
                    self.trapq, next_move_time,
                    move.accel_t, move.cruise_t, move.decel_t,
                    move.start_pos[0], move.start_pos[1], move.start_pos[2],
                    move.axes_r[0], move.axes_r[1], move.axes_r[2],
                    move.start_v, move.cruise_v, move.accel)
            if move.axes_d[3]:
                # NOTE: the extruder stepper move is likely synced to the main
                #       XYZ movement here, by sharing the "next_move_time"
                #       parameter in the call.
                self.extruder.move(print_time=next_move_time, move=move)
            next_move_time = (next_move_time + move.accel_t
                              + move.cruise_t + move.decel_t)
            for cb in move.timing_callbacks:
                # NOTE: execute any "callbacks" registered to be
                #       run at the end of this move.
                cb(next_move_time)
        
        # Generate steps for moves
        if self.special_queuing_state:
            # NOTE: this block is executed when "special_queuing_state" is not None.
            # NOTE: loging "next_move_time" for tracing.
            logging.info("\n\nToolHead _process_moves: " +
                         "calling _update_drip_move_time with " +
                         f"next_move_time={str(next_move_time)}\n\n")
            # NOTE: this function loops "while self.print_time < next_print_time".
            #       It "pauses before sending more steps" using "drip_completion.wait",
            #       and calls "_update_move_time". 
            self._update_drip_move_time(next_move_time)
        
        # NOTE: "next_move_time" is the last "self.print_time" plus the
        #       time added by the new moves sento to trapq.
        #       Here, it is passed to "_update_move_time" (which updates
        #       "self.print_time" and calls "trapq_finalize_moves") and
        #       to overwrite "self.last_kin_move_time".
        logging.info(f"\n\nToolHead _process_moves: _update_move_time with next_move_time={str(next_move_time)}\n\n")
        self._update_move_time(next_move_time)
        logging.info(f"\n\nToolHead _process_moves: last_kin_move_time set to next_move_time={str(next_move_time)}\n\n")
        self.last_kin_move_time = next_move_time
        
    def flush_step_generation(self):
        # Transition from "Flushed"/"Priming"/main state to "Flushed" state
        # NOTE: a "use case" for drip moves is to: 'Exit "Drip" state'

        # NOTE: this is the "flush" method from a "MoveQueue" object.
        #       It calls "_process_moves" on the moves in the queue that
        #       are "ready to be flushed", and removes them from the queue.
        self.move_queue.flush()

        # NOTE: the state is set to "FLushed" which is still a
        #       "special" state (i.e. not the "" main state)
        self.special_queuing_state = "Flushed"
        self.need_check_stall = -1.

        # NOTE: updates the "self._next_timer" object in the "reactor",
        #       and sets "flush_timer.waketime" to "self.reactor.NEVER".
        self.reactor.update_timer(self.flush_timer, self.reactor.NEVER)

        # NOTE: sets "self.junction_flush" to "self.buffer_time_high"
        #       in the MoveQueue class. Note that the "junction_flush"
        #       is reset when the "flush" method is called. Not sure
        #       what this accomplishes.
        self.move_queue.set_flush_time(self.buffer_time_high)

        self.idle_flush_print_time = 0.
        flush_time = self.last_kin_move_time + self.kin_flush_delay
        flush_time = max(flush_time, self.print_time - self.kin_flush_delay)
        # NOTE: this is the only place where "last_kin_flush_time" is updated.
        self.last_kin_flush_time = max(self.last_kin_flush_time, flush_time)
        # NOTE: the following updates "self.print_time" and 
        #       calls "trapq_finalize_moves".
        self._update_move_time(next_print_time=max(self.print_time, 
                                                   self.last_kin_flush_time))
    
    def _flush_lookahead(self):
        if self.special_queuing_state:
            return self.flush_step_generation()
        self.move_queue.flush()
    
    def get_last_move_time(self):
        # NOTE: this method probably returns a "safe" time
        #       which can be used to schedule a new move,
        #       (i.e. after the ).

        # NOTE: The "_flush_lookahead" method calls:
        #       - flush_step_generation: which updates "self.print_time" through "_update_move_time".
        #       - move_queue.flush: also ends up updating "self.print_time".
        self._flush_lookahead()

        # NOTE: the "_calc_print_time" function also updates "self.print_time"
        if self.special_queuing_state:
            self._calc_print_time()
        
        return self.print_time
    
    def _check_stall(self):
        eventtime = self.reactor.monotonic()
        if self.special_queuing_state:
            if self.idle_flush_print_time:
                # Was in "Flushed" state and got there from idle input
                est_print_time = self.mcu.estimated_print_time(eventtime)
                if est_print_time < self.idle_flush_print_time:
                    self.print_stall += 1
                self.idle_flush_print_time = 0.
            # Transition from "Flushed"/"Priming" state to "Priming" state
            self.special_queuing_state = "Priming"
            self.need_check_stall = -1.
            self.reactor.update_timer(self.flush_timer, eventtime + 0.100)
        # Check if there are lots of queued moves and stall if so
        while 1:
            est_print_time = self.mcu.estimated_print_time(eventtime)
            buffer_time = self.print_time - est_print_time
            stall_time = buffer_time - self.buffer_time_high
            if stall_time <= 0.:
                break
            if not self.can_pause:
                self.need_check_stall = self.reactor.NEVER
                return
            eventtime = self.reactor.pause(eventtime + min(1., stall_time))
        if not self.special_queuing_state:
            # In main state - defer stall checking until needed
            self.need_check_stall = (est_print_time + self.buffer_time_high
                                     + 0.100)
    def _flush_handler(self, eventtime):
        try:
            print_time = self.print_time
            buffer_time = print_time - self.mcu.estimated_print_time(eventtime)
            if buffer_time > self.buffer_time_low:
                # Running normally - reschedule check
                return eventtime + buffer_time - self.buffer_time_low
            # Under ran low buffer mark - flush lookahead queue
            self.flush_step_generation()
            if print_time != self.print_time:
                self.idle_flush_print_time = self.print_time
        except:
            logging.exception("Exception in flush_handler")
            self.printer.invoke_shutdown("Exception in flush_handler")
        return self.reactor.NEVER
    
    # Movement commands
    def get_position(self):
        return list(self.commanded_pos)
    
    def set_position(self, newpos, homing_axes=()):
        self.flush_step_generation()
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trapq_set_position(self.trapq, self.print_time,
                                   newpos[0], newpos[1], newpos[2])
        self.commanded_pos[:] = newpos
        
        # NOTE: calls "rail.set_position"/"itersolve_set_position"
        self.kin.set_position(newpos, homing_axes)
        
        self.printer.send_event("toolhead:set_position")
    
    def move(self, newpos, speed):
        move = Move(toolhead=self, 
                    start_pos=self.commanded_pos,
                    end_pos=newpos, 
                    speed=speed)
        # NOTE: So far, the clock time for when this move
        #       will be sent are not known.
        # NOTE: Stepper move commands are not sent with
        #       a "clock" argument.

        # NOTE: move checks.
        if not move.move_d:
            return
        if move.is_kinematic_move:
            self.kin.check_move(move)
        if move.axes_d[3]:
            self.extruder.check_move(move)
        
        # NOTE: update "commanded_pos" with the "end_pos"
        #       of the current move command.
        self.commanded_pos[:] = move.end_pos
        
        # NOTE: add the Move object to the MoveQueue.
        self.move_queue.add_move(move)
        
        if self.print_time > self.need_check_stall:
            self._check_stall()
    
    def manual_move(self, coord, speed):
        # NOTE: the "manual_move" command interprets "None" values
        #       as the latest (commanded) coordinates.
        curpos = list(self.commanded_pos)
        for i in range(len(coord)):
            if coord[i] is not None:
                curpos[i] = coord[i]
        self.move(curpos, speed)
        # NOTE: this event is handled by "reset_last_position"
        #       (at gcode_move.py) which updates "self.last_position"
        #       in the GCodeMove class.
        self.printer.send_event("toolhead:manual_move")
    
    def dwell(self, delay):
        # NOTE: get_last_move_time runs "_flush_lookahead" which then
        #       calls "flush" on the MoveQueue, and ends up calling 
        #       "_update_move_time", which updates "self.print_time".
        #       In essence "get_last_move_time" returns an updated
        #       "self.print_time". The delay is then added to it.
        next_print_time = self.get_last_move_time() + max(0., delay)
        self._update_move_time(next_print_time=next_print_time)
        self._check_stall()
    
    def wait_moves(self):
        self._flush_lookahead()
        eventtime = self.reactor.monotonic()
        while (not self.special_queuing_state
               or self.print_time >= self.mcu.estimated_print_time(eventtime)):
            if not self.can_pause:
                break
            eventtime = self.reactor.pause(eventtime + 0.100)
    def set_extruder(self, extruder, extrude_pos):
        self.extruder = extruder
        self.commanded_pos[3] = extrude_pos
    def get_extruder(self):
        return self.extruder
    
    # Homing "drip move" handling
    def _update_drip_move_time(self, next_print_time):
        # NOTE: called by "_process_moves" when in a "special_queuing_state"
        #       (i.e. when its value is not "" or None).
        flush_delay = DRIP_TIME + self.move_flush_time + self.kin_flush_delay
        while self.print_time < next_print_time:
            # NOTE: "drip_completion.test" is likely a method from "ReactorCompletion",
            #       but is beyond my understanding and deathwishes for spelunking.
            # TODO: ask what it is for!
            if self.drip_completion.test():
                # NOTE: this "exception" does nothing, it "passes",
                #       but it is caught at the "drip_move" method,
                #       which runs "move_queue.reset" and "trapq_finalize_moves"
                #       in response. This must be an "alternate" way to break
                #       the while loop. A bit hacky though.
                raise DripModeEndSignal()
            curtime = self.reactor.monotonic()
            est_print_time = self.mcu.estimated_print_time(curtime)
            wait_time = self.print_time - est_print_time - flush_delay
            if wait_time > 0. and self.can_pause:
                # Pause before sending more steps
                self.drip_completion.wait(curtime + wait_time)
                continue
            npt = min(self.print_time + DRIP_SEGMENT_TIME, next_print_time)
            # NOTE: this updates "self.print_time" and calls "trapq_finalize_moves",
            #       possibly to "Generate steps for moves".
            self._update_move_time(next_print_time=npt)
            # NOTE: because how "print_time" is updated, the while loop will end
            #       before "self.print_time >= next_print_time" by "MOVE_BATCH_TIME".
    
    def drip_move(self, newpos, speed, drip_completion):
        # NOTE: "drip_completion=all_endstop_trigger" is 
        #       probably made from "reactor.completion" objects.
        self.dwell(self.kin_flush_delay)
        # Transition from "Flushed"/"Priming"/main state to "Drip" state
        self.move_queue.flush()
        self.special_queuing_state = "Drip"
        self.need_check_stall = self.reactor.NEVER
        self.reactor.update_timer(self.flush_timer, self.reactor.NEVER)
        self.move_queue.set_flush_time(self.buffer_time_high)
        self.idle_flush_print_time = 0.
        self.drip_completion = drip_completion
        
        # Submit move
        try:
            # NOTE: uses "add_move", to add a move to the "move_queue".
            # NOTE: logging for tracing activity
            logging.info("\n\ndrip_move: sending move to the queue.\n\n")
            self.move(newpos, speed)
        except self.printer.command_error as e:
            self.flush_step_generation()
            raise
        
        # Transmit move in "drip" mode
        try:
            # NOTE: because the flush function is called with a 
            #       not None "special_queuing_state", the "_process_moves" 
            #       call will use "_update_drip_move_time".
            # NOTE: logging for tracing activity
            logging.info("\n\ndrip_move: flushing move queue / transmitting move.\n\n")
            self.move_queue.flush()
        except DripModeEndSignal as e:
            logging.info("\n\ndrip_move: resetting move queue / DripModeEndSignal caught.\n\n")
            # NOTE: deletes al moves in the queue
            self.move_queue.reset()
            # NOTE: This calls a function in "trapq.c", described as:
            #       - Expire any moves older than `print_time` from the trapezoid velocity queue
            #       - Flush all moves from trapq (in the case of print_time=NEVER_TIME)
            #       I am guessing here that "older" means "with a smaller timestamp",
            #       otherwise it does not make sense.
            self.trapq_finalize_moves(self.trapq, self.reactor.NEVER)
            
            # NOTE: the above may be specific to toolhead and not to extruder...
            #       Add an "event" that calls this same method on the 
            #       extruder trapq as well.
            #self.printer.send_event("toolhead:trapq_finalize_extruder_drip_moves", 
            #                        self.reactor.NEVER, self.extruder.name)
            # NOTE: Alternatively, use the "update_move_time" of the extruder object.
            #       This function calls "trapq_finalize_moves(self.trapq, flush_time)"
            #       on the extruder's trapq.
            # TODO: Whether it will mess with XYZ-only homing or not remains to be tested.
            self.extruder.update_move_time(flush_time=self.reactor.NEVER)
        
        # Exit "Drip" state
        # NOTE: logging for tracing activity
        logging.info("\n\ndrip_move: calling flush_step_generation / exit drip state.\n\n")
        # NOTE: the "flush_step_generation" method, which calls:
        #       - "flush", which should do nothing (dine just above, and the queue is empty).
        #       - "reactor.update_timer"
        #       - "move_queue.set_flush_time"
        #       - "_update_move_time"
        # NOTE: pausing the program here prevented the "second home" move
        #       issue during homing the extruder with a drip move. The solution
        #       was to also call "trapq_finalize_moves" on the extruder's "trapq"
        #       above, and just before "flush_step_generation" below.
        self.flush_step_generation()
    
    # Misc commands
    def stats(self, eventtime):
        for m in self.all_mcus:
            m.check_active(self.print_time, eventtime)
        buffer_time = self.print_time - self.mcu.estimated_print_time(eventtime)
        is_active = buffer_time > -60. or not self.special_queuing_state
        if self.special_queuing_state == "Drip":
            buffer_time = 0.
        return is_active, "print_time=%.3f buffer_time=%.3f print_stall=%d" % (
            self.print_time, max(buffer_time, 0.), self.print_stall)
    def check_busy(self, eventtime):
        est_print_time = self.mcu.estimated_print_time(eventtime)
        lookahead_empty = not self.move_queue.queue
        return self.print_time, est_print_time, lookahead_empty
    def get_status(self, eventtime):
        print_time = self.print_time
        estimated_print_time = self.mcu.estimated_print_time(eventtime)
        res = dict(self.kin.get_status(eventtime))
        res.update({ 'print_time': print_time,
                     'stalls': self.print_stall,
                     'estimated_print_time': estimated_print_time,
                     'extruder': self.extruder.get_name(),
                     'position': self.Coord(*self.commanded_pos),
                     'max_velocity': self.max_velocity,
                     'max_accel': self.max_accel,
                     'max_accel_to_decel': self.requested_accel_to_decel,
                     'square_corner_velocity': self.square_corner_velocity})
        return res
    def _handle_shutdown(self):
        self.can_pause = False
        self.move_queue.reset()
    def get_kinematics(self):
        return self.kin
    def get_trapq(self):
        return self.trapq
    def register_step_generator(self, handler):
        self.step_generators.append(handler)
    def note_step_generation_scan_time(self, delay, old_delay=0.):
        self.flush_step_generation()
        cur_delay = self.kin_flush_delay
        if old_delay:
            self.kin_flush_times.pop(self.kin_flush_times.index(old_delay))
        if delay:
            self.kin_flush_times.append(delay)
        new_delay = max(self.kin_flush_times + [SDS_CHECK_TIME])
        self.kin_flush_delay = new_delay
    def register_lookahead_callback(self, callback):
        last_move = self.move_queue.get_last()
        if last_move is None:
            callback(self.get_last_move_time())
            return
        last_move.timing_callbacks.append(callback)
    def note_kinematic_activity(self, kin_time):
        self.last_kin_move_time = max(self.last_kin_move_time, kin_time)
    def get_max_velocity(self):
        return self.max_velocity, self.max_accel
    def _calc_junction_deviation(self):
        scv2 = self.square_corner_velocity**2
        self.junction_deviation = scv2 * (math.sqrt(2.) - 1.) / self.max_accel
        self.max_accel_to_decel = min(self.requested_accel_to_decel,
                                      self.max_accel)
    def cmd_G4(self, gcmd):
        # Dwell
        delay = gcmd.get_float('P', 0., minval=0.) / 1000.
        self.dwell(delay)
    def cmd_M400(self, gcmd):
        # Wait for current moves to finish
        self.wait_moves()
    cmd_SET_VELOCITY_LIMIT_help = "Set printer velocity limits"
    def cmd_SET_VELOCITY_LIMIT(self, gcmd):
        max_velocity = gcmd.get_float('VELOCITY', None, above=0.)
        max_accel = gcmd.get_float('ACCEL', None, above=0.)
        square_corner_velocity = gcmd.get_float(
            'SQUARE_CORNER_VELOCITY', None, minval=0.)
        requested_accel_to_decel = gcmd.get_float(
            'ACCEL_TO_DECEL', None, above=0.)
        if max_velocity is not None:
            self.max_velocity = max_velocity
        if max_accel is not None:
            self.max_accel = max_accel
        if square_corner_velocity is not None:
            self.square_corner_velocity = square_corner_velocity
        if requested_accel_to_decel is not None:
            self.requested_accel_to_decel = requested_accel_to_decel
        self._calc_junction_deviation()
        msg = ("max_velocity: %.6f\n"
               "max_accel: %.6f\n"
               "max_accel_to_decel: %.6f\n"
               "square_corner_velocity: %.6f" % (
                   self.max_velocity, self.max_accel,
                   self.requested_accel_to_decel,
                   self.square_corner_velocity))
        self.printer.set_rollover_info("toolhead", "toolhead: %s" % (msg,))
        if (max_velocity is None and
            max_accel is None and
            square_corner_velocity is None and
            requested_accel_to_decel is None):
            gcmd.respond_info(msg, log=False)
    def cmd_M204(self, gcmd):
        # Use S for accel
        accel = gcmd.get_float('S', None, above=0.)
        if accel is None:
            # Use minimum of P and T for accel
            p = gcmd.get_float('P', None, above=0.)
            t = gcmd.get_float('T', None, above=0.)
            if p is None or t is None:
                gcmd.respond_info('Invalid M204 command "%s"'
                                  % (gcmd.get_commandline(),))
                return
            accel = min(p, t)
        self.max_accel = accel
        self._calc_junction_deviation()

def add_printer_objects(config):
    config.get_printer().add_object('toolhead', ToolHead(config))
    kinematics.extruder.add_printer_objects(config)
