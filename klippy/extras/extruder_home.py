# By naikymen and mdwasp
# Original idea at: https://discord.com/channels/627982902738681876/1046618170993160202/1046808809894588457
# Relevatn issue: https://gitlab.com/pipettin-bot/pipettin-grbl/-/issues/47#note_1215525244
# Module distributed under the terms of the GNU GPL v3 licence.
#
#
# This class loads the stepper of the active extruder and performs homing on it.
# It is inspired by the manual_stepper module.
# A config section is required to activate it, and a command must be sent to use it.
# The module requires a modification in "extruder.py", which will create the extruder
# stepper from the PrinterRail class, instead of the PrinterStepper class, when an
# "endstop_pin" is defined in the extruder's config.
#
# See "config/configs-pipetting-bot/configs-mainsail/printer.cfg" for an example config.

import stepper, chelper, logging
from toolhead import Move
from collections import namedtuple

class ExtruderHoming:
    """
    ! WARNING EXPERIMENTAL UNTESTED
    This class registers a command to home an extruder's stepper.
    This is made possible due to a change in the Extruder class,
    which can now add an endstop to the extruder stepper, if a
    config parameter is provided.

    The "toolhead" passed to the PrinterHoming.manual_home method
    is not entirely "virtual". All methods are defined here, but
    some of them call the methods of the actual toolhead.
    """
    def __init__(self, config):
        self.printer = config.get_printer()
        self.extruder_name = config.get_name().split()[1]

        self.toolhead = None
        self.extruder = None
        self.gcmd = None
        self.th_orig_pos = None

        self.HOMING_DELAY = 0.001

        # NOTE: some parameters are loaded from the "extruder_homing" config section.
        self.velocity = config.getfloat('velocity', 5., above=0.)
        self.accel = self.homing_accel = config.getfloat('accel', 0., minval=0.)

        # TODO: find out what this is. The same is used in manual_stepper.py
        self.next_cmd_time = 0.
        
        # NOTE: The following command will become available with the syntax:
        #       "HOME_EXTRUDER EXTRUDER=extruder", the extruder name
        #        passed to the EXTRUDER argument might change.
        # Register commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command('HOME_EXTRUDER', "EXTRUDER",
                                   self.extruder_name, self.cmd_HOME_EXTRUDER,
                                   desc=self.cmd_HOME_EXTRUDER_help)
    
    # NOTE: the "register_mux_command" above registered a "HOME_EXTRUDER"
    #       command, which will end up calling this method.
    #       The "help" string is usually defined along the method.
    cmd_HOME_EXTRUDER_help = "Home an extruder using an endstop. The active extruder will be homed."
    def cmd_HOME_EXTRUDER(self, gcmd):
        
        # Get gcmd object, for later.
        self.gcmd = gcmd
        
        # NOTE: Get the toolhead and its extruder
        self.toolhead = self.printer.lookup_object("toolhead")
        self.extruder = self.toolhead.get_extruder()            # PrinterExtruder
        self.extruder_trapq = self.extruder.get_trapq()         # extruder trapq (from ffi)
        
        # NOTE: Get the steppers
        self.extruder_stepper = self.extruder.extruder_stepper  # ExtruderStepper
        self.rail = self.extruder_stepper.rail                  # PrinterRail
        self.stepper = self.extruder_stepper.stepper            # PrinterRail or PrinterStepper
        self.steppers = [self.stepper]                          # [PrinterRail or PrinterStepper]
        # NOTE: in the "ExtruderStepper" class, the "rail" and the "stepper"  
        #       objects are _the same_ object.

        # NOTE: get the endstops from the extruder's PrinterRail.
        #       likely a list of tuples, each with an instance of 
        #       MCU_endstop and a stepper name.
        #       See PrinterRail at stepper.py.
        endstops = self.rail.get_endstops()                 # [(mcu_endstop, name)]
        
        # NOTE: get a PrinterHoming class from extras
        phoming = self.printer.lookup_object('homing')      # PrinterHoming

        # NOTE: Get toolhead position 
        self.th_orig_pos = self.toolhead.get_position()

        # NOTE: get homing information, speed and move coordinate.
        homing_info = self.rail.get_homing_info()
        speed = homing_info.speed
        # NOTE: Use XYZ from the toolhead, and E from the config file + estimation.
        pos = self.th_orig_pos[:3] + [self.get_movepos(homing_info)]
        
        # NOTE: "manual_home" is defined in the PrinterHoming class (at homing.py).
        #       The method instantiates a "HomingMove" class by passing it the
        #       "endstops" and "toolhead" objects. Here, the "self" object is passed
        #       as a "virtual toolhead", similar to what is done in manual_stepper.
        #       The provided endstops are from the extruder PrinterRail.
        # NOTE: "PrinterHoming.manual_home" then calls "HomingMove.homing_move".
        #       The "HomingMove" class downstream methods use the
        #       following methods from a provided "toolhead" object:
        #       - flush_step_generation
        #       - get_kinematics:           returning a "kin" object with methods:
        #           - kin.get_steppers:     returning a list of stepper objects.
        #           - kin.calc_position:    returning ???
        #       - get_position:             returning "thpos" (toolhead position)
        #       - get_last_move_time:       returning "print_time" (and later "move_end_print_time")
        #       - dwell
        #       - drip_move
        #       - set_position
        # NOTE: Other methods using the toolhead object or derivatives are also called:
        #       -   calc_toolhead_pos: This method receives a "movepos" argument,
        #           which is the "pos" list above:  pos = [0., 0., 0., 0.]
        # NOTE: Of these methods, the Extruder class defines none.
        # NOTE: The object returned by "get_kinematics" is
        #       required to have the following methods:
        #       - get_steppers()
        #       - calc_position(kin_spos)
        # NOTE: The following command ends up calling the methods 
        #       in this class. For example "drip_move" for moving
        #       the extruder (towards the endstop, ideally).
        # NOTE: There are also other methods for homing:
        #       - probing_move ???
        #       - cmd_G28: ???
        # TODO: consider using those alternative methods.
        phoming.manual_home(toolhead=self, endstops=endstops,
                            pos=pos, speed=speed,
                            # NOTE: argument passed to "mcu_endstop.home_start",
                            #       and used directly in the low-level command.
                            triggered=True, 
                            # NOTE: if True, an "error" is recorded when the move
                            #       completes without the endstop triggering.
                            check_triggered=True)

        # NOTE: Update positions in gcode_move, fixes inaccurate first
        #       relative move. Might not be needed since actually using 
        #       set_position from the TH.
        #       Might interfere with extruder move?
        # gcode_move = self.printer.lookup_object('gcode_move')
        # gcode_move.reset_last_position()

    def get_movepos(self, homing_info):
        # NOTE: based on "_home_axis" from CartKinematics, it estimates
        #       the distance to move for homing, at least for a G28 command.
        # Determine movement
        position_min, position_max = self.rail.get_range()  # 0, 100
        movepos = homing_info.position_endstop
        if homing_info.positive_dir:
            movepos -= 1.5 * (homing_info.position_endstop - position_min)  #   (0 - 0)*1.5 = 0
        else:
            movepos += 1.5 * (position_max - homing_info.position_endstop)  # (100 - 0)*1.5 = 150

        logging.info(f"\n\nget_movepos: movepos={str(movepos)}\n\n")
        return movepos

    def get_kinematics(self):
        """
        Virtual toolhead method.
        Called by:
            -   HomingMove.calc_toolhead_pos
            -   HomingMove.homing_move
        """
        # TEST: identical to manual_stepper, 
        #       methods for a "virtual kin" are defined here.
        return self
    
    def get_steppers(self):
        """
        Virtual toolhead method.
        Called by:
            -   HomingMove.calc_toolhead_pos,
            -   HomingMove.homing_move

        """
        # TEST: passes extruder stepper (list)
        return self.steppers
    
    # TODO: Is this method from manual_stepper required?
    def sync_print_time(self):
        # NOTE: this function is likely making the "toolhead"
        #       wait for all moves to end before doing something
        #       else. Perhaps it will be called by "manual_home" below.
        #toolhead = self.printer.lookup_object('toolhead')
        print_time = self.toolhead.get_last_move_time()
        if self.next_cmd_time > print_time:
            self.toolhead.dwell(self.next_cmd_time - print_time)
        else:
            self.next_cmd_time = print_time

    # Toolhead wrappers to support homing
    def flush_step_generation(self):
        """
        Virtual toolhead method.
        Called by:
            -   HomingMove.homing_move
        """
        # TODO: What should I do here?
        #       I think it is allright to use the toolheads method,
        #       because the extruder stepper is part of the toolhead.
        #       Also, the alternative "sync_print_time" does not make
        #       much sense to me, because it was used to get the latest
        #       print time, to be used in other manual_stepper methods.
        #self.sync_print_time()
        self.toolhead.flush_step_generation()

    def get_last_move_time(self):
        """
        Virtual toolhead method.
        Called by:
            -   HomingMove.homing_move
        """
        # TODO: What should I do here? Using toolhead method.
        # self.sync_print_time()
        # return self.next_cmd_time
        lmt = self.toolhead.get_last_move_time()
        logging.info(f"\n\nget_last_move_time: Last move time: {str(lmt)}\n\n")
        return lmt
    
    def dwell(self, delay):
        """
        Virtual toolhead method.
        Called by:
            -   HomingMove.homing_move
        """
        # TODO: What should I do here? Using toolhead method.
        #self.next_cmd_time += max(0., delay)
        # NOTE: once upon a time, there was no "drip_move" for homing.
        #       so a "dwell" was added to give an old RPi2 enough time
        #       to compute stuff:
        #       - https://github.com/Klipper3d/klipper/commit/78f4c25a14099564cf731bdaf5b97492a3a6fb47
        #       When the drip move was introduced, the dwell was significantly reduced:
        #       - https://github.com/Klipper3d/klipper/commit/dd34768e3afb6b5aa46885109182973d88df10b7
        #       Here the drip_move is _not_ used, and we thus require the
        #       extended dwell time, thereby ignoring the delay argument.

        # NOTE: pass the input (default)
        self.HOMING_DELAY = delay

        # NOTE: From homing.py
        # self.HOMING_DELAY = 0.001
        
        # NOTE: The original pre-drip value
        # self.HOMING_DELAY = 0.250
        
        # NOTE: the 0.250 valued did not work at all,
        #       so it was increased by a nice amount,
        #       and now... It works! OMG :D Sometimes...
        # self.HOMING_DELAY = 4.0

        logging.info(f"\n\ndwell: Dwelling for {str(self.HOMING_DELAY)} before homing. Current print_time: {str(self.toolhead.print_time)}\n\n")
        self.toolhead.dwell(self.HOMING_DELAY)
        logging.info(f"\n\ndwell: Done sending dwell command. Current print_time: {str(self.toolhead.print_time)}\n\n")
    
    def move_extruder(self, newpos, speed, drip_completion):
        """
        This method is an alternative to implement "drip_move",
        using the Extruder.move command. To use it instead of other
        methods in this class, uncomment it in the "drip_move" method,
        and comment the others.
        """
        # NOTE: Explanation of drip_move arguments:
        #       - newpos: new position vector [0,0,0,0]
        #       - speed:  ???
        #       - drip_completion: ???
        # NOTE: some explanation on "drip" moves is available at:
        #       https://github.com/Klipper3d/klipper/commit/43064d197d6fd6bcc55217c5e9298d86bf4ecde7
        # NOTE: the manual_stepper class simply "moves" the stepper 
        #       in the regular way. However the ToolHead.drip_move does
        #       a lot more, in accordance with the commit linked above.
        # self.do_move(newpos[0], speed, self.homing_accel)
        # TODO: What should I do here? What should the Move argument be?
        #       The "Extruder.move" method requires the following arguments:
        #       print_time: ???
        #       move: ???
        curpos = list(self.toolhead.commanded_pos)
        # move = Move(toolhead=self.toolhead, 
        #             start_pos=curpos,
        #             end_pos=curpos[:3] + [25.0],  # TODO: base this on the config
        #             speed=self.velocity)
        ntMove = namedtuple('Move', "axes_r accel start_v cruise_v axes_d accel_t cruise_t decel_t start_pos end_pos")
        move = ntMove(axes_r=[0,0,0,1.0], accel=20.0, start_v=0.0, cruise_v=20.0, axes_d=[None,None,None,25.0],
                      accel_t=1.0, cruise_t=5.0, decel_t=1.0, start_pos=[0.0,0.0,0.0,0.0], end_pos=[0.0,0.0,0.0,0.0])
        print_time = self.toolhead.print_time  # TODO: this should be OK if the dwell above is enough? IDK
        self.extruder.move(print_time=print_time, move=move)
        # NOTE: the following is done automatically by the end of the move method.
        # self.extruder.last_position = 0

        next_move_time = (print_time + move.accel_t + move.cruise_t + move.decel_t)

        self.toolhead._update_move_time(next_move_time)
        self.toolhead.last_kin_move_time = next_move_time

        pass
    
    def move_toolhead(self, newpos, speed, drip_completion):
        """
        This method is an alternative to implement "drip_move",
        using the ToolHead.manual_move command. To use it instead of other
        methods in this class, uncomment it in the "drip_move" method,
        and comment the others.
        """
        # NOTE: Explanation of drip_move arguments:
        #       - newpos: new position vector [0,0,0,0]
        #       - speed:  ???
        #       - drip_completion: ???
        # NOTE: The manual_move method allows "None" values to be passed,
        #       allowing me not to worry about getting the current and new
        #       coordinates for the homing move.
        
        # NOTE: using "toolhead.manual_move" allows None values, is simpler
        #       to use in that sense, and also ends up calling "toolhead.move".
        # extra = 0.0
        # e_newpos = newpos[3] + extra
        # coord = [None, None, None, e_newpos]
        # logging.info(f"\n\nmove_toolhead: Moving {self.extruder.name} to {str(coord)} for homing.\n\n")  # Can be [None, None, None, 0.0]
        # self.toolhead.manual_move(coord=coord, speed=speed)

        # NOTE: using "toolhead.move" should be similar.
        logging.info(f"\n\nmove_toolhead: moving toolhead to {str(newpos)} for homing.\n\n")
        self.toolhead.move(newpos=newpos, speed=speed)
        logging.info(f"\n\nmove_toolhead: move completed.\n\n")

    def move_toolhead_drip(self, newpos, speed, drip_completion):
        """
        This method passes argument to the real toolhead "drip_move" method.
        """

        logging.info(f"\n\nmove_toolhead_drip: drip-moving to {str(newpos)} for homing.\n\n")  # Can be [None, None, None, 0.0]
        self.toolhead.drip_move(newpos, speed, drip_completion)

    def move_forced(self, newpos, speed, drip_completion):
        """
        This method uses MANUAL_MOVE from force_move.py
        """
        # NOTE: try brute force?
        force_move = self.printer.lookup_object("force_move")
        self.stepper = force_move.manual_move(self.stepper, dist=10.0, speed=speed, accel=100.0)


    def drip_move(self, newpos, speed, drip_completion):
        """
        Virtual toolhead method.
        Called by:
            -   HomingMove.homing_move
        """
        # NOTE: the "drip_completion" argument is an object that can be checked
        #       for to know if all of the endstops have been triggered yet.

        # NOTE: option 1, use the "manual_move" method from the ToolHead class.
        # TODO: Couldn't debug "Timer too close" nor "flush_handler" errors (at or after homing).
        #self.move_toolhead(newpos, speed, drip_completion)
        
        # NOTE: option 2, use the "move" method from the Extruder class.
        # TODO: Fails after homing with "Exception in flush_handler" / "Invalid sequence".
        # self.move_extruder(newpos, speed, drip_completion)

        # NOTE: option 3, use the "drop move" method from the ToolHead class.
        # TODO: It's strange that the stepper stops at the endstop, and then moves a bit more... it shouldn't!
        self.move_toolhead_drip(newpos, speed, drip_completion)

        # NOTE: option 4, out of ideas.
        #       This works flawlessly.
        # self.move_forced(newpos, speed, drip_completion)

    def get_position(self):
        """
        Virtual toolhead method.
        Called by :
            -   _calc_endstop_rate
            -   calc_toolhead_pos
        """
        # NOTE: calc_toolhead_pos only uses the fourth element from this list,
        #       which is the extruder position. The equivalent coordinates from
        #       the toolhead are obtained from "get_commanded_position" of the
        #       steppers in the toolhead kinematics object, which ultimately
        #       come from the "ffi" objects.
        #       TODO: check that this is the correct method to get the position.
        #           Another option is "last_position" from PrinterExtruder, which
        #           is updated at the end of a move command (after the trapq_append).
        e_pos = self.rail.get_commanded_position()      # NOTE: option 1
        # e_pos = self.extruder.last_position           # NOTE: option 2

        pos = self.th_orig_pos[:3] + [e_pos]
        logging.info(f"\n\nget_position output: {str(pos)}\n\n")
        return pos
    
    def set_position(self, newpos, homing_axes=()):
        """
        Virtual toolhead method.
        Called by:
            -   HomingMove.homing_move
        """
        # TODO: What should I do here?
        # NOTE: I am assuming that the "set_position" applies to steppers.
        #       by tracing calls to the "set_position" method in MCU_stepper.
        #       There, the "coords" argument is a list of at least 3 components:
        #           [coord[0], coord[1], coord[2]]
        #       I do not know why it needs three components for a steper object,
        #       but from the "itersolve_set_position" code, they seem to be x,y,z 
        #       components.
        # self.do_set_position(newpos[0])
        # NOTE: the set_position method in a toolhead calls set_position in a
        #       PrinterRail object, which we have here. That method calls 
        #       the set_position method in each of the steppers in the rail.
        # NOTE: At this point, this method receives a vector: 
        #           "[4.67499999999994, 0.0, 0.0, 3.3249999999999402]"
        #                newpos[0]                    newpos[3]
        #       The first 3 items come from the "calc_position" method below.
        #       The first item is the "updated" position of the extruder stepper,
        #       corresponding to "haltpos", and the second and third are hard-coded 0s.
        #       The fourth component comes from "rail.get_commanded_position" here,
        #       which may correspond to the "old" extruder position, meaning that the
        #       rail has not been updated yet (sensibly because set_position is called later).
        #       This means that we need the original 123 toolhead components, and
        #       only update the remaining extruder component using the toolhead.set_position
        #       method. However it only sets the XYZ components in the XYZ "trapq".

        # NOTE:
        #       -   The "negative"/"retract" E direction is currently "downwards".
        #       -   The "positive"/"extrude" E direction is currently "upwards".
        #       -   The axis is currently homing "upwards" (to the "positive" direction).
        
        # TODO: changing this affects the sencond stepper move after homing. Find out why.
        #       Using newpos[0] always showed the second move. Using newpos[3] only shows
        #       a second move the first time.
        # newpos_e = newpos[0]    # haltpos=calc_toolhead_pos (at homing.py), example values: [1.995, 0.0, 0.0, 0.0] 
        # newpos_e = newpos[3]    # rail.get_commanded_position (calls ffi_lib.itersolve_get_commanded_pos at stepper.py)
        newpos_e = 0.0          # just zero
        # ! Forcing "0" here means that the corrected "haltpos" is ignored.
        
        # NOTE: Log stuff
        logging.info(f"\n\nset_position: input={str(newpos)} homing_axes={str(homing_axes)}\n\n")
        logging.info(f"\n\nset_position: old TH position={str(self.th_orig_pos)}\n\n")
        pos = self.th_orig_pos[:3] + [newpos_e]
        logging.info(f"\n\nset_position: output={str(pos)}\n\n")

        # TODO: I need to update the "E" queue somehow.
        #       When switching extruder steppers, the Extruder class uses the set_position
        #       method in its stepper object, passing it: [extruder.last_position, 0., 0.]
        #       Thus, I need a three element list, where the first element is the updated position.

        # logging.info(f"\n\nset_position input: {str(newpos)}\n\n")
        # coord = [newpos[0], 0.0, 0.0]
        # logging.info(f"\n\nset_position output: {str(coord)}\n\n")
        # self.rail.set_position(coord)

        # NOTE: The next line from toolhead.py is: "self.flush_step_generation()"
        #       Update extruder position, code adapted from "set_position" in toolhead.py.
        # NOTE: The "flush_step_generation" toolhead method runs
        #       "trapq_finalize_moves" on the extruder's "trapq" as well.
        #       No need to do it here, hopefully.
        self.toolhead.flush_step_generation()
        ffi_main, ffi_lib = chelper.get_ffi()
        # NOTE: Dice "// Note a position change in the trapq history" en "trapq.c".
        ffi_lib.trapq_set_position(self.extruder_trapq, 
                                   # TODO: check source for print-time is correct
                                   self.toolhead.print_time,
                                   newpos_e, 0., 0.)
        
        # NOTE: The next line from toolhead.py is: "self.commanded_pos[:] = newpos".
        #       The most similar line from extruder.py is in "sync_to_extruder",
        #       from the ExtruderStepper class:
        #           self.stepper.set_position([extruder.last_position, 0., 0.])
        #self.extruder_stepper.stepper.set_position([newpos_e, 0., 0.])
        # NOTE: Since the above line is equivalent to the next line , I've commented it.
        #       Furthermore, "commanded_pos" *will* be updated in the toolhead by the
        #       last line below, calling "toolhead.set_position".

        # NOTE: The next line from toolhead.py is: "self.kin.set_position(newpos, homing_axes)"
        #       It calls "rail.set_position" on all toolhead's rails (which are PrinterRails),
        #       and updates the toolhead limits using "rail.get_range".
        #       Those rails are "PrinterRail" classes, which in turn call
        #       "stepper.set_position" on each of their steppers.
        #       It calls "itersolve_set_position". Replicate here:
        self.rail.set_position([newpos_e, 0., 0.])
        # NOTE: note that this position will be then read by the 
        #       call to "stepper.get_commanded_position" in "homing.py".

        # NOTE: The next line in toolhead.py is: self.printer.send_event("toolhead:set_position")
        #       It runs the "reset_last_position" method in GCodeMove at "gcode_move.py",
        #       which takes no arguments. It runs the "position_with_transform" method,
        #       which is apparently "toolhead.get_position" (simply returning "commanded_pos").
        # NOTE: Thus, I can simply call "set_position" from the toolhead, that does all of the above.
        #       This is expected for a complete regular homing move, guessing it shouldn't hurt.
        self.toolhead.set_position(pos)

        logging.info(f"\n\nset_position: final TH position={str(self.toolhead.get_position())}\n\n")
        pass
    
    def calc_position(self, stepper_positions):
        """
        Virtual toolhead method.
        Called by HomingMove.calc_toolhead_pos
        """
        # TODO: What should I do here?
        #       The manual_stepper code is similar to the CartKinematics method.
        # NOTE: The get_name function is inherited from the
        #       first stepper in the steppers list of the
        #       PrinterRail class.
        # NOTE: This code was grabbed from the cartesian.py calc_position method.
        pos = [stepper_positions[self.rail.get_name()], 0., 0.]
        logging.info(f"\n\ncalc_position input calc_position={str(stepper_positions)}\n\n")
        logging.info(f"\n\ncalc_position return pos={str(pos)}\n\n")
        return pos

def load_config_prefix(config):
    return ExtruderHoming(config)
