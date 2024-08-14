#!/usr/bin/python

# Raspberry Pi GPIO-controlled video looper
# Modified to use python-vlc instead of omxplayer

import RPi.GPIO as GPIO
import os
import sys
import time
from threading import Lock
import signal
import argparse
import vlc


class _GpioParser(argparse.Action):
    """ Parse a GPIO spec string (see argparse setup later in this file) """
    def __call__(self, parser, namespace, values, option_string=None):
        gpio_dict = {}
        pin_pairs = values.split(',')
        for pair in pin_pairs:
            pair_split = pair.split(':')

            if 0 == len(pair_split) > 2:
                raise ValueError('Invalid GPIO pin format')

            try:
                in_pin = int(pair_split[0])
            except ValueError:
                raise ValueError('GPIO input pin must be numeric integer')

            try:
                out_pin = int(pair_split[1])
            except ValueError:
                raise ValueError('GPIO output pin must be numeric integer')
            except IndexError:
                out_pin = None

            if in_pin in gpio_dict:
                raise ValueError('Duplicate GPIO input pin: {}'.format(in_pin))

            gpio_dict[in_pin] = out_pin

        setattr(namespace, self.dest, gpio_dict)


class VidLooper(object):
    _GPIO_BOUNCE_TIME = 200
    _VIDEO_EXTS = ('.mp4', '.m4v', '.mov', '.avi', '.mkv')
    _GPIO_PIN_DEFAULT = {
        26: 21,
        19: 20,
        13: 16,
        6: 12
    }

    _mutex = Lock()
    _active_vid = None
    _player = None

    def __init__(self, audio='hdmi', autostart=True, restart_on_press=False,
                 video_dir=os.getcwd(), videos=None, gpio_pins=None, loop=True,
                 no_osd=False, shutdown_pin=None, splash=None, debug=False):
        if gpio_pins is None:
            gpio_pins = self._GPIO_PIN_DEFAULT.copy()
        self.gpio_pins = gpio_pins
        self.shutdown_pin = shutdown_pin

        if videos:
            self.videos = videos
            for video in videos:
                if not os.path.exists(video):
                    raise FileNotFoundError('Video "{}" not found'.format(video))
        else:
            self.videos = [os.path.join(video_dir, f)
                           for f in sorted(os.listdir(video_dir))
                           if os.path.splitext(f)[1] in self._VIDEO_EXTS]
            if not self.videos:
                raise Exception('No videos found in "{}". Please specify a different '
                                'directory or filename(s).'.format(video_dir))

        assert len(self.videos) <= len(self.gpio_pins), \
            "Not enough GPIO pins configured for number of videos"

        self.debug = debug
        self.audio = audio
        self.autostart = autostart
        self.restart_on_press = restart_on_press
        self.loop = loop
        self.no_osd = no_osd
        self.splash = splash
        self._splashproc = None

        self._instance = vlc.Instance('--aout={}'.format(self.audio))
        self._player = self._instance.media_player_new()

    def _kill_process(self):
        """ Stop the VLC player """
        if self._player is not None:
            self._player.stop()

    def switch_vid(self, pin):
        """ Switch to the video corresponding to the shorted pin """
        with self._mutex:
            for in_pin, out_pin in self.gpio_pins.items():
                if out_pin is not None:
                    GPIO.output(out_pin,
                                GPIO.HIGH if in_pin == pin else GPIO.LOW)

            filename = self.videos[self.in_pins.index(pin)]
            if filename != self._active_vid or self.restart_on_press:
                print (filename)
                self._kill_process()
                media = self._instance.media_new(filename)
                self._player.set_media(media)
                self._player.play()

                if self.loop:
                    self._player.set_media(media)
                    self._player.get_media().add_option("input-repeat=-1")

                self._active_vid = filename

    @property
    def in_pins(self):
        """ Create a tuple of input pins, for easy access """
        return tuple(self.gpio_pins.keys())

    def start(self):
        if not self.debug:
            os.system('clear')
            os.system('tput civis')

        GPIO.setmode(GPIO.BCM)
        for in_pin, out_pin in self.gpio_pins.items():
            GPIO.setup(in_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            if out_pin is not None:
                GPIO.setup(out_pin, GPIO.OUT)
                GPIO.output(out_pin, GPIO.LOW)

        if self.shutdown_pin:
            GPIO.setup(self.shutdown_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(self.shutdown_pin,
                                  GPIO.FALLING,
                                  callback=lambda _: call(['shutdown', '-h', 'now'], shell=False),
                                  bouncetime=self._GPIO_BOUNCE_TIME)

        if self.autostart:
            if self.splash is not None:
                self._splashproc = Popen(['fbi', '--noverbose', '-a',
                                          self.splash])
            else:
                self.switch_vid(self.in_pins[0])

        for pin in self.in_pins:
            GPIO.add_event_detect(pin, GPIO.FALLING, callback=self.switch_vid,
                                  bouncetime=self._GPIO_BOUNCE_TIME)

        try:
            while True:
                time.sleep(0.5)
                if not self.loop:
                    if self._player is not None and self._player.get_state() == vlc.State.Ended:
                        for out_pin in self.gpio_pins.values():
                            if out_pin is not None:
                                GPIO.output(out_pin, GPIO.LOW)
                        self._active_vid = None

        finally:
            self.__del__()

    def __del__(self):
        if not self.debug:
            os.system('tput cnorm')

        GPIO.cleanup()
        self._kill_process()

        if self._splashproc:
            os.killpg(os.getpgid(self._splashproc.pid), signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="""Raspberry Pi video player controlled by GPIO pins

This program is designed to power a looping video display, where the active
video can be changed by pressing a button (i.e. by shorting a GPIO pin).
The active video can optionally be indicated by an LED (one output for each
input pin; works well with switches with built-in LEDs, but separate LEDs work
too).

This video player uses python-vlc to control video playback.
"""
    )
    parser.add_argument('--audio', default='hdmi',
                        choices=('hdmi', 'local', 'both'),
                        help='Output audio over HDMI, local (headphone jack),'
                             'or both')
    parser.add_argument('--no-autostart', action='store_false',
                        dest='autostart', default=True,
                        help='Don\'t start playing a video on startup')
    parser.add_argument('--no-loop', action='store_false', default=True,
                        dest='loop', help='Don\'t loop the active video')
    parser.add_argument(
        '--restart-on-press', action='store_true', default=False,
        help='If True, restart the current video if the button for the active '
             'video is pressed. If False, pressing the button for the active '
             'video will be ignored.')
    vidmode = parser.add_mutually_exclusive_group()
    vidmode.add_argument(
        '--video-dir', default=os.getcwd(),
        help='Directory containing video files. Use this or specify videos one '
             'at a time at the end of the command.')
    vidmode.add_argument('videos', action="store", nargs='*', default=(),
                         help='List of video paths (local, rtsp:// or rtmp://)')
    parser.add_argument('--gpio-pins', default=VidLooper._GPIO_PIN_DEFAULT,
                        action=_GpioParser,
                        help='List of GPIO pins. Either INPUT:OUTPUT pairs, or '
                             'just INPUT pins (no output), separated by '
                             'commas.')
    parser.add_argument('--debug', action='store_true', default=False,
                        help='Debug mode (don\'t clear screen or suppress '
                             'terminal output)')
    parser.add_argument('--countdown', type=int, default=0,
                        help='Add a countdown before start (time in seconds)')
    parser.add_argument('--splash', type=str, default=None,
                        help='Splash screen image to show when no video is '
                             'playing')
    parser.add_argument('--no-osd', action='store_true', default=False,
                        help='Don\'t show on-screen display when changing '
                             'videos')
    parser.add_argument('--shutdown-pin', type=int, default=None,
                        help='GPIO pin to trigger system shutdown (default None)')

    args = parser.parse_args()

    countdown = args.countdown

    while countdown > 0:
        sys.stdout.write(
            '\rrpi-vidlooper starting in {} seconds '
            '(Ctrl-C to abort)...'.format(countdown))
        sys.stdout.flush()
        time.sleep(1)
        countdown -= 1

    del args.countdown

    VidLooper(**vars(args)).start()


if __name__ == '__main__':
    main()
