"""Screen/window/region video recording via the ScreenCast portal + GStreamer.

COSMIC implements ext-image-copy-capture (not wlr-screencopy), so wf-recorder &
co. don't work. The sanctioned path is the freedesktop ScreenCast portal: it
hands us a PipeWire node we encode with GStreamer. The portal also natively
picks a monitor or a window (so App Window / Screen need no overlay of ours);
Region records the chosen monitor and crops to the rectangle.

Flow: CreateSession -> SelectSources(type) -> Start (user consents in the
compositor's picker) -> OpenPipeWireRemote (fd) -> pipewiresrc -> H.264 -> mp4.
"""
from __future__ import annotations

import sys
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gio", "2.0")
# Pin the GTK stack to 3.x so the lazy `from gi.repository import Gtk/Gdk` calls
# in this module don't grab GTK 4 (which would clash with the loaded Gdk 3.0).
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gio, GLib, Gst  # noqa: E402

_BUS = "org.freedesktop.portal.Desktop"
_OBJ = "/org/freedesktop/portal/desktop"
_SC = "org.freedesktop.portal.ScreenCast"
_REQ = "org.freedesktop.portal.Request"

# SelectSources types bitmask / cursor modes (portal spec).
SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
CURSOR_EMBEDDED = 2

_Gst_inited = False


def _gst():
    global _Gst_inited
    if not _Gst_inited:
        Gst.init(None)
        _Gst_inited = True


def _have(element: str) -> bool:
    return Gst.ElementFactory.find(element) is not None


def _h264_chain() -> str:
    """H.264 encoder + parser, hardware VAAPI first, software after."""
    if _have("vah264enc"):
        return "vah264enc ! h264parse"
    if _have("openh264enc"):
        return "openh264enc ! h264parse"
    if _have("x264enc"):
        return "x264enc tune=zerolatency speed-preset=veryfast ! h264parse"
    raise RuntimeError("No H.264 encoder (install gstreamer1.0-vaapi or "
                       "gstreamer1.0-plugins-ugly).")


def _aac_chain():
    """AAC encoder (+ parser when available), or None if neither is installed."""
    if _have("avenc_aac"):
        enc = "avenc_aac"
    elif _have("voaacenc"):
        enc = "voaacenc"
    else:
        return None
    return enc + (" ! aacparse" if _have("aacparse") else "")


class ScreenCastPortal:
    """Runs the ScreenCast portal handshake on the default GLib main context."""

    def __init__(self, source_type: int):
        self._type = source_type
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        sender = self._bus.get_unique_name()[1:].replace(".", "_")
        self._sender = sender
        self._n = 0
        self._session = None
        self._on_ready = None
        self._on_error = None

    # -- request/response plumbing ---------------------------------------
    def _token(self):
        self._n += 1
        return f"cs{self._n}"

    def _request(self, method, args, options, on_response, fd_list=None):
        token = self._token()
        options["handle_token"] = GLib.Variant("s", token)
        path = f"/org/freedesktop/portal/desktop/request/{self._sender}/{token}"
        state = {}

        def handler(_c, _s, _p, _i, _sig, params):
            self._bus.signal_unsubscribe(state["sub"])
            try:
                code, results = params.unpack()
                on_response(code, results)
            except Exception as exc:  # never leave the main loop hung on a stuck
                import traceback      # handshake (it holds the capture lock)
                traceback.print_exc()
                if self._on_error:
                    self._on_error(f"portal handshake failed: {exc}")

        state["sub"] = self._bus.signal_subscribe(
            _BUS, _REQ, "Response", path, None, Gio.DBusSignalFlags.NONE, handler)

        full = list(args) + [options]
        variant = GLib.Variant(self._sig(method), tuple(full))
        self._bus.call_with_unix_fd_list(
            _BUS, _OBJ, _SC, method, variant, GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE, -1, fd_list, None, lambda *_: None)

    @staticmethod
    def _sig(method):
        return {
            "CreateSession": "(a{sv})",
            "SelectSources": "(oa{sv})",
            "Start": "(osa{sv})",
        }[method]

    # -- handshake steps -------------------------------------------------
    def start(self, on_ready, on_error):
        self._on_ready, self._on_error = on_ready, on_error
        opts = {"session_handle_token": GLib.Variant("s", self._token())}
        try:
            self._request("CreateSession", [], opts, self._created)
        except Exception as exc:
            on_error(f"portal CreateSession failed: {exc}")

    def _created(self, code, results):
        if code != 0 or "session_handle" not in results:
            return self._on_error("Recording was cancelled.")
        self._session = results["session_handle"]
        opts = {
            "types": GLib.Variant("u", self._type),
            "multiple": GLib.Variant("b", False),
            "cursor_mode": GLib.Variant("u", CURSOR_EMBEDDED),
        }
        self._request("SelectSources", [self._session], opts, self._selected)

    def _selected(self, code, _results):
        if code != 0:
            return self._on_error("Recording was cancelled.")
        self._request("Start", [self._session, ""], {}, self._started)

    def _started(self, code, results):
        if code != 0:
            return self._on_error("Recording was cancelled.")
        streams = results.get("streams") or []
        if not streams:
            return self._on_error("No screen source was selected.")
        node_id, props = streams[0]
        self._open_pipewire(node_id, props)

    def _open_pipewire(self, node_id, props):
        variant = GLib.Variant("(oa{sv})",
                               (self._session, {}))
        self._bus.call_with_unix_fd_list(
            _BUS, _OBJ, _SC, "OpenPipeWireRemote", variant,
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None,
            self._pw_opened, (node_id, props))

    def _pw_opened(self, conn, res, user_data):
        node_id, props = user_data
        try:
            ret, fd_list = conn.call_with_unix_fd_list_finish(res)
            idx = ret.unpack()[0]
            fd = fd_list.get(idx)
        except Exception as exc:
            return self._on_error(f"OpenPipeWireRemote failed: {exc}")
        self._on_ready(fd, node_id, props)


class Recorder:
    """Encodes a PipeWire screencast node to an H.264 mp4, with optional crop."""

    def __init__(self, out_path: str):
        _gst()
        self.out_path = out_path
        self.pipeline = None
        self._loop_quit = None
        self._crop = None
        self._crop_fraction = None

    def _encoder_chain(self):
        return _h264_chain()

    def _audio_chain(self):
        """A pulsesrc -> AAC branch feeding the named mux, or None if no AAC
        encoder is installed (then we record video-only)."""
        aac = _aac_chain()
        if aac is None:
            print("[record] no AAC encoder — recording without audio",
                  file=sys.stderr, flush=True)
            return None
        return (f"pulsesrc name=asrc do-timestamp=true ! queue ! audioconvert ! "
                f"audioresample ! {aac} ! queue ! mux.")

    def build(self, fd: int, node_id: int, audio_device=None):
        # videocrop is always present (pass-through when not cropping). For a
        # region we set its borders from the ACTUAL negotiated frame size once
        # caps are known (see set_crop_fraction) — the portal reports a logical
        # size but the PipeWire frames arrive at device resolution, so a crop in
        # logical coords would land in the wrong place on scaled displays.
        audio = self._audio_chain() if audio_device else None
        desc = (
            f"pipewiresrc fd={fd} path={node_id} do-timestamp=true keepalive-time=1000 "
            f"! videorate ! video/x-raw,framerate=30/1 ! videoconvert ! "
            f"videocrop name=crop ! {self._encoder_chain()} ! "
            f"mp4mux name=mux faststart=true ! filesink name=sink"
        )
        if audio:
            desc += " " + audio
        self.pipeline = Gst.parse_launch(desc)
        # Set the path on the element (NOT in the parse string): parse_launch is
        # not a shell, so any quoting/spaces in the path would corrupt it.
        self.pipeline.get_by_name("sink").set_property("location", self.out_path)
        if audio:
            self.pipeline.get_by_name("asrc").set_property("device", audio_device)
        self._crop = self.pipeline.get_by_name("crop")

    def set_crop_fraction(self, fraction):
        """Crop to a region given as (fx, fy, fw, fh) fractions of the recorded
        monitor. Applied to the real frame size once the caps negotiate."""
        self._crop_fraction = fraction
        pad = self._crop.get_static_pad("sink")
        pad.connect("notify::caps", self._apply_crop)
        self._apply_crop(pad, None)  # in case caps are already set

    def _apply_crop(self, pad, _pspec):
        if not self._crop_fraction:
            return
        caps = pad.get_current_caps()
        if caps is None or caps.get_size() == 0:
            return
        s = caps.get_structure(0)
        ok_w, W = s.get_int("width")
        ok_h, H = s.get_int("height")
        if not (ok_w and ok_h) or W <= 0 or H <= 0:
            return
        fx, fy, fw, fh = self._crop_fraction
        left = max(0, min(W - 2, round(fx * W)))
        top = max(0, min(H - 2, round(fy * H)))
        width = max(2, round(fw * W))
        height = max(2, round(fh * H))
        right = max(0, W - left - width)
        bottom = max(0, H - top - height)
        # H.264 needs even dimensions; nudge the far edge if the kept area is odd.
        if (W - left - right) % 2:
            right += 1
        if (H - top - bottom) % 2:
            bottom += 1
        self._crop.set_property("left", left)
        self._crop.set_property("top", top)
        self._crop.set_property("right", right)
        self._crop.set_property("bottom", bottom)
        print(f"[record] crop applied on {W}x{H} frame: "
              f"left={left} top={top} right={right} bottom={bottom}",
              file=sys.stderr, flush=True)
        self._crop_fraction = None  # apply once

    def play(self):
        # Watch the bus so an encoder/negotiation error during recording is
        # surfaced instead of silently producing an empty file.
        self.error = None
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        bus.connect("message::warning", self._on_bus_warning)
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        print(f"[record] pipeline -> PLAYING: {ret.value_nick}", file=sys.stderr, flush=True)

    def _on_bus_error(self, _bus, msg):
        err, dbg = msg.parse_error()
        self.error = err.message
        print(f"[record] GST ERROR: {err.message} | {dbg}", file=sys.stderr, flush=True)

    def _on_bus_warning(self, _bus, msg):
        err, dbg = msg.parse_warning()
        print(f"[record] GST WARN: {err.message} | {dbg}", file=sys.stderr, flush=True)

    def stop(self) -> None:
        """Send EOS and wait for the mux to finalise the mp4, then tear down."""
        if self.pipeline is None:
            return
        # If the pipeline never reached PLAYING (e.g. an error), there's no EOS
        # coming — tear down immediately instead of blocking for 8s.
        _ok, state, _pending = self.pipeline.get_state(0)
        if self.error or state != Gst.State.PLAYING:
            print(f"[record] not PLAYING ({state.value_nick}) — skipping EOS wait",
                  file=sys.stderr, flush=True)
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            return
        print("[record] sending EOS, waiting for mux to finalise…", file=sys.stderr, flush=True)
        self.pipeline.send_event(Gst.Event.new_eos())
        bus = self.pipeline.get_bus()
        msg = bus.timed_pop_filtered(8 * Gst.SECOND,
                                     Gst.MessageType.EOS | Gst.MessageType.ERROR)
        print(f"[record] EOS wait result: {msg.type.value_nicks if msg else 'TIMEOUT'}",
              file=sys.stderr, flush=True)
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None



class _RegionDim:
    """Dim everything except the recorded region (per monitor, click-through),
    with a red frame around the region, so the user keeps seeing what's being
    captured. Click-through, so windows underneath stay usable; the dim/frame
    sit outside the crop, so they're never in the video."""

    def __init__(self, region, monitors):
        self.windows = []
        from gi.repository import Gdk
        from .overlay import _DimWindow
        disp = Gdk.Display.get_default()
        for m in monitors:
            try:
                w = _DimWindow(m, disp.get_monitor(m.index), region)
                self.windows.append(w); w.show_all()
            except Exception:
                pass

    def destroy(self):
        for w in self.windows:
            try:
                w.destroy()
            except Exception:
                pass
        self.windows = []


def _uri(path: str) -> str:
    """file:// URI for a local path — via GLib, so spaces and #? survive."""
    try:
        return Gst.filename_to_uri(path)
    except Exception:
        return "file://" + path


def _has_audio_track(path: str) -> bool:
    try:
        gi.require_version("GstPbutils", "1.0")
        from gi.repository import GstPbutils
        info = GstPbutils.Discoverer.new(5 * Gst.SECOND).discover_uri(_uri(path))
        return bool(info.get_audio_streams())
    except Exception as exc:
        print(f"[trim] audio probe failed ({exc}) — assuming video only",
              file=sys.stderr, flush=True)
        return False


def trim_clip(src: str, dst: str, start_ns: int, end_ns: int, on_progress=None):
    """Write src[start_ns:end_ns] to dst. Returns None on success, else a
    human-readable error string (the caller then keeps the untrimmed clip).

    Re-encodes rather than stream-copies on purpose: the hardware H.264 encoder
    emits barely any keyframes (a 10 s capture can hold a single one), so a
    copy-mode cut would snap the in-point back to the start of the file. Same
    reason the seek below is ACCURATE.

    `on_progress(fraction | None)` is called every ~100 ms; it must pump the GTK
    main loop itself if it draws anything.
    """
    import os
    _gst()
    try:
        vchain = _h264_chain()
    except RuntimeError as exc:
        return str(exc)
    aac = _aac_chain()
    audio = aac is not None and _has_audio_track(src)
    # decodebin's pads appear late, so each branch starts with an element that
    # accepts ONE medium (videoconvert / audioconvert). Starting a branch with a
    # `queue` instead would let the deferred link put audio in the video branch,
    # since a queue accepts anything.
    desc = (f"filesrc name=src ! decodebin name=dec "
            f"dec. ! videoconvert ! queue ! {vchain} ! "
            f"mp4mux name=mux faststart=true ! filesink name=sink")
    if audio:
        desc += f" dec. ! audioconvert ! audioresample ! queue ! {aac} ! mux."
    try:
        pipe = Gst.parse_launch(desc)
    except Exception as exc:
        return f"could not build the trim pipeline: {exc}"
    # Paths go on the elements, never in the parse string: parse_launch is not a
    # shell, so spaces in a filename would corrupt the description.
    pipe.get_by_name("src").set_property("location", src)
    pipe.get_by_name("sink").set_property("location", dst)

    err = None
    try:
        pipe.set_state(Gst.State.PAUSED)
        # Preroll before seeking: a seek on a not-yet-prerolled pipeline is
        # simply refused.
        if pipe.get_state(10 * Gst.SECOND)[0] == Gst.StateChangeReturn.FAILURE:
            return "the clip could not be opened for trimming"
        # A normal (non-SEGMENT) seek with a stop time makes the pipeline emit
        # EOS at `end_ns`, which is also what makes mp4mux finalise the file.
        # After a flushing seek, running time restarts at 0, so the output
        # starts at the in-point rather than carrying its old timestamps.
        #
        # Sent to the demuxer, not the pipeline: a pipeline-wide seek also
        # reaches filesink, whose segment is in BYTES, and it answers a TIME
        # seek with a GStreamer-CRITICAL. Same cut either way, minus the noise.
        seek = Gst.Event.new_seek(
            1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET, int(start_ns), Gst.SeekType.SET, int(end_ns))
        if not pipe.get_by_name("dec").send_event(seek):
            return "the trim points could not be applied to this clip"
        pipe.set_state(Gst.State.PLAYING)
        span = max(1, int(end_ns) - int(start_ns))
        # Generous: re-encoding runs faster than real time on any encoder we
        # pick, so this only ever fires on a genuinely stuck pipeline.
        deadline = time.monotonic() + 60 + 20 * (span / Gst.SECOND)
        bus = pipe.get_bus()
        while True:
            msg = bus.timed_pop_filtered(
                100 * Gst.MSECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if msg is not None:
                if msg.type == Gst.MessageType.ERROR:
                    gerr, dbg = msg.parse_error()
                    print(f"[trim] GST ERROR: {gerr.message} | {dbg}",
                          file=sys.stderr, flush=True)
                    err = gerr.message
                break
            if time.monotonic() > deadline:
                err = "trimming timed out"
                break
            if on_progress is not None:
                ok, pos = pipe.query_position(Gst.Format.TIME)
                on_progress((pos - start_ns) / span if ok and pos >= 0 else None)
    finally:
        pipe.set_state(Gst.State.NULL)

    if err is None and (not os.path.exists(dst) or os.path.getsize(dst) < 1024):
        err = "the trimmed file came out empty"
    if err is not None:
        try:
            if os.path.exists(dst):
                os.unlink(dst)
        except OSError:
            pass
    return err


class _TrimProgress:
    """Modal 'Trimming…' bar shown while trim_clip() runs."""

    def __init__(self, parent):
        from gi.repository import Gtk
        self._Gtk = Gtk
        self.dlg = Gtk.Dialog(title="Trimming", transient_for=parent, modal=True)
        self.dlg.set_deletable(False)
        self.dlg.set_default_size(320, -1)
        box = self.dlg.get_content_area()
        box.set_spacing(10)
        for setter in ("set_margin_top", "set_margin_bottom",
                       "set_margin_start", "set_margin_end"):
            getattr(box, setter)(14)
        box.add(Gtk.Label(label="Trimming the recording…"))
        self.bar = Gtk.ProgressBar()
        box.add(self.bar)
        self.dlg.show_all()
        self.pump()

    def __call__(self, fraction):
        if fraction is None:
            self.bar.pulse()
        else:
            self.bar.set_fraction(max(0.0, min(1.0, fraction)))
        self.pump()

    def pump(self):
        # We're called from a button handler inside Gtk.main(); iterate by hand
        # so the bar actually moves. The dialog is modal, so nothing else in the
        # app can be clicked while we do.
        while self._Gtk.events_pending():
            self._Gtk.main_iteration_do(False)

    def destroy(self):
        self.dlg.destroy()
        self.pump()


def _rounded(cr, x, y, w, h, r):
    import math
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class _TrimBar:
    """QuickTime-style trim strip: a scrubbable timeline whose two ends can be
    dragged inward to choose what is kept. What falls outside is dimmed; the
    kept range carries a yellow frame with a grip at each end.

    Deliberately not a Gtk.DrawingArea subclass: this module imports Gtk lazily
    so it can pin the GTK 3 stack, and subclassing would need it at import time.
    """

    HEIGHT = 46
    PAD = 12              # room for a handle when a trim point sits at an end
    HANDLE_W = 11
    GRAB = 14             # px around a trim point where a press grabs it
    MIN_SEL_NS = 300 * Gst.MSECOND

    def __init__(self, on_seek, on_trim):
        from gi.repository import Gdk, Gtk
        self._on_seek = on_seek        # (ns, dragging) -> None
        self._on_trim = on_trim        # () -> None, after a handle is released
        self.duration = 0
        self.pos = 0
        self.start = 0
        self.end = 0
        self._drag = None              # None | "start" | "end" | "pos"
        self._cursor = None
        w = Gtk.DrawingArea()
        w.set_size_request(-1, self.HEIGHT)
        w.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                     | Gdk.EventMask.BUTTON_RELEASE_MASK
                     | Gdk.EventMask.POINTER_MOTION_MASK
                     | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        w.connect("draw", self._draw)
        w.connect("button-press-event", self._press)
        w.connect("button-release-event", self._release)
        w.connect("motion-notify-event", self._motion)
        w.connect("leave-notify-event", self._leave)
        w.set_tooltip_text("Click or drag to scrub · drag either yellow end to "
                           "trim · [ and ] trim to the playhead")
        self.widget = w

    # -- state ------------------------------------------------------------
    @property
    def trimmed(self) -> bool:
        return self.duration > 0 and (self.start > 0 or self.end < self.duration)

    @property
    def selection_ns(self) -> int:
        return max(0, self.end - self.start)

    def set_duration(self, ns):
        """Called once the demuxer reports the length; keeps any trim the user
        already made (they can't have made one before this, but be safe)."""
        first = self.duration <= 0
        self.duration = ns
        if first or self.end > ns:
            self.end = ns
        self.start = max(0, min(self.start, max(0, ns - self.MIN_SEL_NS)))
        self.widget.queue_draw()

    def set_position(self, ns):
        self.pos = ns
        self.widget.queue_draw()

    def reset(self):
        self.start, self.end = 0, self.duration
        self.widget.queue_draw()
        self._on_trim()

    def trim_to_playhead(self, which):
        """`[` / `]`: pull the near end in to where the playhead sits."""
        if self.duration <= 0:
            return
        if which == "start":
            self.start = max(0, min(self.pos, self.end - self.MIN_SEL_NS))
        else:
            self.end = min(self.duration, max(self.pos, self.start + self.MIN_SEL_NS))
        self.widget.queue_draw()
        self._on_trim()

    # -- geometry ---------------------------------------------------------
    def _track(self):
        a = self.widget.get_allocation()
        return self.PAD, max(self.PAD + 1, a.width - self.PAD), a.height

    def _x(self, ns):
        x0, x1, _h = self._track()
        if self.duration <= 0:
            return x0
        return x0 + (x1 - x0) * max(0.0, min(1.0, ns / self.duration))

    def _ns(self, x):
        x0, x1, _h = self._track()
        if x1 <= x0 or self.duration <= 0:
            return 0
        return int(self.duration * max(0.0, min(1.0, (x - x0) / (x1 - x0))))

    # -- drawing ----------------------------------------------------------
    def _draw(self, _w, cr):
        x0, x1, h = self._track()
        top, bot = 4.0, h - 4.0
        if bot <= top:
            return False
        xs, xe = self._x(self.start), self._x(self.end)

        _rounded(cr, x0, top, x1 - x0, bot - top, 5)
        cr.set_source_rgb(0.15, 0.16, 0.19)          # empty track
        cr.fill()
        cr.rectangle(xs, top, max(1.0, xe - xs), bot - top)
        cr.set_source_rgb(0.30, 0.32, 0.38)          # the part being kept
        cr.fill()
        cr.set_source_rgba(0, 0, 0, 0.45)            # the parts being cut
        if xs > x0:
            cr.rectangle(x0, top, xs - x0, bot - top)
        if xe < x1:
            cr.rectangle(xe, top, x1 - xe, bot - top)
        cr.fill()

        cr.set_source_rgb(1.0, 0.84, 0.04)           # yellow trim frame
        cr.set_line_width(3)
        for y in (top + 1.5, bot - 1.5):
            cr.move_to(xs, y)
            cr.line_to(xe, y)
        cr.stroke()
        # Handles sit OUTSIDE the selection, so they can never overlap each
        # other however tight the trim gets.
        for hx, left in ((xs, True), (xe, False)):
            gx = hx - self.HANDLE_W if left else hx
            _rounded(cr, gx, top, self.HANDLE_W, bot - top, 4)
            cr.set_source_rgb(1.0, 0.84, 0.04)
            cr.fill()
            cr.set_source_rgb(0.25, 0.20, 0.0)       # grip lines
            cr.set_line_width(1.5)
            mid = (top + bot) / 2
            for dx in (-2.0, 2.0):
                cr.move_to(gx + self.HANDLE_W / 2 + dx, mid - 6)
                cr.line_to(gx + self.HANDLE_W / 2 + dx, mid + 6)
            cr.stroke()

        if self.duration > 0:                        # playhead
            px = round(self._x(max(self.start, min(self.end, self.pos)))) + 0.5
            cr.set_line_width(3)
            cr.set_source_rgba(0, 0, 0, 0.5)
            cr.move_to(px, top)
            cr.line_to(px, bot)
            cr.stroke()
            cr.set_line_width(1.5)
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(px, top)
            cr.line_to(px, bot)
            cr.stroke()
        return False

    # -- input ------------------------------------------------------------
    def _grabbable(self, x):
        if self.duration <= 0:
            return None
        d_s, d_e = abs(x - self._x(self.start)), abs(x - self._x(self.end))
        if min(d_s, d_e) > self.GRAB:
            return None
        return "start" if d_s <= d_e else "end"

    def _press(self, _w, ev):
        if ev.button != 1 or self.duration <= 0:
            return False
        self._drag = self._grabbable(ev.x) or "pos"
        self._apply(ev.x)
        return True

    def _motion(self, _w, ev):
        if self._drag is None:
            self._hover(ev.x)
            return False
        self._apply(ev.x)
        return True

    def _release(self, _w, ev):
        if self._drag is None:
            return False
        was, self._drag = self._drag, None
        self._on_seek(self.pos, False)     # land exactly where the drag ended
        if was in ("start", "end"):
            self._on_trim()
        self.widget.queue_draw()
        return True

    def _apply(self, x):
        ns = self._ns(x)
        if self._drag == "start":
            self.start = max(0, min(ns, self.end - self.MIN_SEL_NS))
            self.pos = self.start
        elif self._drag == "end":
            self.end = min(self.duration, max(ns, self.start + self.MIN_SEL_NS))
            self.pos = self.end          # show the frame you're cutting at
        else:
            self.pos = max(self.start, min(self.end, ns))
        self.widget.queue_draw()
        self._on_seek(self.pos, True)

    def _hover(self, x):
        from gi.repository import Gdk
        win = self.widget.get_window()
        if win is None:
            return
        name = "col-resize" if self._grabbable(x) else "pointer"
        if name == self._cursor:
            return
        self._cursor = name
        try:
            win.set_cursor(Gdk.Cursor.new_from_name(win.get_display(), name))
        except Exception:
            pass

    def _leave(self, *_):
        win = self.widget.get_window()
        if win is not None and self._cursor is not None:
            self._cursor = None
            try:
                win.set_cursor(None)
            except Exception:
                pass
        return False


class PreviewWindow:
    """Plays the just-recorded clip and offers Save / Discard. The timeline
    doubles as a QuickTime-style trim bar, so only the kept range is written on
    save. Closing without saving asks for confirmation."""

    def __init__(self, path, on_save, on_discard, suggested_name="recording.mp4",
                 start_dir=None):
        from gi.repository import Gtk
        _gst()
        self.path = path
        self._on_save = on_save
        self._on_discard = on_discard
        self._suggested_name = suggested_name
        self._start_dir = start_dir
        self.win = Gtk.Window(title="CosmicShot — Recording")
        self.win.set_default_size(900, 560)
        self.win.set_position(Gtk.WindowPosition.CENTER)
        self.win.connect("delete-event", self._on_close)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.win.add(box)

        self._playbin = Gst.ElementFactory.make("playbin", None)
        sink = Gst.ElementFactory.make("gtksink", None)
        video = None
        if sink is not None:
            self._playbin.set_property("video-sink", sink)
            video = sink.get_property("widget")
        self._playbin.set_property("uri", _uri(path))
        if video is not None:
            box.pack_start(video, True, True, 0)
        else:
            box.pack_start(Gtk.Label(label="(preview unavailable)"), True, True, 0)

        # --- timeline: playhead + QuickTime-style trim handles ---
        self._duration = 0          # ns, 0 until the demuxer reports it
        self._seeking = False       # true while the user drags on the bar
        tl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tl.set_margin_start(10); tl.set_margin_end(10); tl.set_margin_top(6)
        self._bar = _TrimBar(self._bar_seek, self._trim_changed)
        tl.pack_start(self._bar.widget, True, True, 0)
        self._time_lbl = Gtk.Label(label="0:00 / 0:00")
        self._time_lbl.set_width_chars(12)
        tl.pack_start(self._time_lbl, False, False, 0)
        box.pack_start(tl, False, False, 0)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(6); bar.set_margin_bottom(8)
        bar.set_margin_start(10); bar.set_margin_end(10)
        self._play_btn = Gtk.Button(label="⏸ Pause")
        self._play_btn.set_tooltip_text("Play / pause (Space)")
        self._play_btn.connect("clicked", self._toggle_play)
        bar.pack_start(self._play_btn, False, False, 0)
        restart = Gtk.Button(label="↺ Restart")
        restart.set_tooltip_text("Back to the start of the kept range")
        restart.connect("clicked", lambda _b: self._restart())
        bar.pack_start(restart, False, False, 0)
        self._reset_btn = Gtk.Button(label="Reset trim")
        self._reset_btn.set_tooltip_text("Keep the whole recording again")
        self._reset_btn.set_sensitive(False)
        self._reset_btn.connect("clicked", lambda _b: self._bar.reset())
        bar.pack_start(self._reset_btn, False, False, 0)
        self._sel_lbl = Gtk.Label()
        self._sel_lbl.set_margin_start(4)
        bar.pack_start(self._sel_lbl, False, False, 0)
        bar.pack_end(self._save_btn(), False, False, 0)
        discard = Gtk.Button(label="Discard")
        discard.get_style_context().add_class("destructive-action")
        discard.connect("clicked", lambda _b: self._discard())
        bar.pack_end(discard, False, False, 0)
        box.pack_start(bar, False, False, 0)

        self.win.connect("key-press-event", self._on_key)

        self._bus = self._playbin.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message::eos", self._on_eos)
        self._bus.connect("message::error", lambda *_: None)
        self._poll_id = None
        self._ended = False
        self._seek_src = None        # pending throttled seek (GLib source id)
        self._seek_target = None     # newest requested position
        self._settle_until = 0.0     # ignore reported positions until this time

    # -- timeline ---------------------------------------------------------
    @staticmethod
    def _fmt(ns):
        secs = max(0, int(ns // Gst.SECOND))
        return f"{secs // 60}:{secs % 60:02d}"

    @staticmethod
    def _fmt_exact(ns):
        """Tenths, for the trim readout — whole seconds there would round the two
        ends and the length independently and read like bad arithmetic."""
        tenths = max(0, int(round(ns / (Gst.SECOND / 10))))
        return f"{tenths // 600}:{tenths // 10 % 60:02d}.{tenths % 10}"

    def _bar_seek(self, ns, dragging):
        """The trim bar moved the playhead (scrub, or a handle being dragged)."""
        self._seeking = dragging
        self._seek_ns(ns)
        if not dragging:
            self._flush_seek()      # land exactly where the drag ended

    def _trim_changed(self):
        """A trim handle was released, or the trim was reset."""
        b = self._bar
        self._reset_btn.set_sensitive(b.trimmed)
        self._sel_lbl.set_markup(
            f"<small>keeping {self._fmt_exact(b.selection_ns)} "
            f"({self._fmt_exact(b.start)} – {self._fmt_exact(b.end)})</small>"
            if b.trimmed else "")
        self._save_button.set_label("Save trimmed" if b.trimmed else "Save")
        # Nudge the playhead back inside the kept range if a handle passed it.
        if not (b.start <= self._position() <= b.end):
            self._seek_ns(min(max(self._position(), b.start), b.end))

    # Coalesce the flood of seeks a drag produces, and ignore reported positions
    # for a moment afterwards so a stale one can't yank the playhead back.
    _SEEK_THROTTLE_MS = 50
    _SEEK_SETTLE_S = 0.2

    def _seek_ns(self, pos):
        """Move to `pos`, showing that exact frame even while paused.

        Seeks are ACCURATE, not KEY_UNIT: the hardware H.264 encoder emits
        barely any keyframes (a 10 s capture can hold a single one), so
        keyframe-snapped seeks collapsed the whole timeline onto one position
        and scrubbing did nothing.

        Accurate seeks cost more, so while the playhead is being dragged they
        are throttled rather than issued per motion event. The playhead and
        clock move immediately regardless, so dragging always feels live.
        """
        self._ended = False       # an explicit seek means "play from here"
        if self._duration:
            pos = max(0, min(self._duration, pos))
        self._seek_target = pos
        self._bar.set_position(pos)
        self._refresh_time(pos)
        if self._seeking:                       # mid-drag: coalesce
            if self._seek_src is None:
                self._seek_src = GLib.timeout_add(self._SEEK_THROTTLE_MS,
                                                  self._flush_seek)
        else:                                   # a click/key/button: go now
            self._flush_seek()

    def _flush_seek(self):
        if self._seek_src is not None:
            GLib.source_remove(self._seek_src)
            self._seek_src = None
        pos, self._seek_target = self._seek_target, None
        if pos is None or self._playbin is None:
            return False
        try:
            self._playbin.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, pos)
        except Exception:
            pass
        self._settle_until = time.monotonic() + self._SEEK_SETTLE_S
        return False              # never repeat as a timeout source

    def _refresh_time(self, pos):
        self._time_lbl.set_text(f"{self._fmt(pos)} / {self._fmt(self._duration)}")

    def _position(self):
        """Best current position: the pending seek target if one is in flight,
        otherwise where the playhead is drawn."""
        return self._seek_target if self._seek_target is not None else self._bar.pos

    def _poll(self):
        """Keep the playhead and the time label in step with the player."""
        if self._playbin is None:
            return False
        if self._duration <= 0:
            ok, dur = self._playbin.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                self._duration = dur
                self._bar.set_duration(dur)
                self._refresh_time(0)
        if (self._ended or self._seeking or self._seek_target is not None
                or time.monotonic() < self._settle_until):
            return True          # parked, dragging, or just sought: hands off
        ok, pos = self._playbin.query_position(Gst.Format.TIME)
        if not ok or pos < 0:
            return True
        # Playback stays inside the trim: stop at the out-point instead of
        # running on through material that won't be saved.
        if self._duration and pos >= self._bar.end:
            self._park_at_end()
            return True
        self._bar.set_position(pos)
        self._refresh_time(pos)
        return True

    def _on_key(self, _w, ev):
        from gi.repository import Gdk
        if ev.keyval == Gdk.KEY_space:
            self._toggle_play(None)
            return True
        if ev.keyval == Gdk.KEY_bracketleft:
            self._bar.trim_to_playhead("start")
            return True
        if ev.keyval == Gdk.KEY_bracketright:
            self._bar.trim_to_playhead("end")
            return True
        return False

    def _save_btn(self):
        from gi.repository import Gtk
        b = Gtk.Button(label="Save")
        b.get_style_context().add_class("suggested-action")
        b.connect("clicked", lambda _b: self._save())
        self._save_button = b
        return b

    def _toggle_play(self, _b):
        _ok, state, _ = self._playbin.get_state(0)
        if state == Gst.State.PLAYING:
            self._playbin.set_state(Gst.State.PAUSED)
            self._play_btn.set_label("▶ Play")
        else:
            if self._ended:          # parked on the out-point -> play again
                self._seek_ns(self._bar.start)
            self._playbin.set_state(Gst.State.PLAYING)
            self._play_btn.set_label("⏸ Pause")

    def _restart(self):
        self._seek_ns(self._bar.start)
        self._playbin.set_state(Gst.State.PLAYING)
        self._play_btn.set_label("⏸ Pause")

    def _park_at_end(self):
        """Hold on the out-point instead of running past it or looping.

        It used to seek back to 0 and keep playing at EOS, which restarted the
        recording under you while you were still looking at the end of it.
        """
        self._playbin.set_state(Gst.State.PAUSED)
        self._ended = True
        end = self._bar.end if self._duration else 0
        if self._duration:
            self._bar.set_position(end)
            self._refresh_time(end)
        self._play_btn.set_label("▶ Play")

    def _on_eos(self, *_):
        self._park_at_end()

    def _stop_player(self):
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        if self._seek_src is not None:
            GLib.source_remove(self._seek_src)
            self._seek_src = None
        try:
            self._playbin.set_state(Gst.State.NULL)
        except Exception:
            pass

    def _save(self):
        from gi.repository import Gtk
        dlg = Gtk.FileChooserDialog(
            title="Save recording", transient_for=self.win,
            action=Gtk.FileChooserAction.SAVE)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Save", Gtk.ResponseType.ACCEPT)
        dlg.set_do_overwrite_confirmation(True)
        dlg.set_current_name(self._suggested_name)
        if self._start_dir:
            try:
                dlg.set_current_folder(self._start_dir)
            except Exception:
                pass
        flt = Gtk.FileFilter()
        flt.set_name("MP4 video")
        flt.add_pattern("*.mp4")
        dlg.add_filter(flt)
        resp = dlg.run()
        chosen = dlg.get_filename() if resp == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        if not chosen:
            return  # cancelled — keep the preview open so they can try again
        if not chosen.lower().endswith(".mp4"):
            chosen += ".mp4"
        # Release the file before re-encoding it.
        self._stop_player()
        src = self.path
        if self._bar.trimmed:
            out = self.path + ".trim.mp4"
            prog = _TrimProgress(self.win)
            try:
                err = trim_clip(self.path, out, self._bar.start, self._bar.end,
                                on_progress=prog)
            finally:
                prog.destroy()
            if err is None:
                src = out
            else:
                warn = Gtk.MessageDialog(
                    transient_for=self.win, modal=True,
                    message_type=Gtk.MessageType.WARNING,
                    buttons=Gtk.ButtonsType.OK,
                    text="Couldn't trim the recording")
                warn.format_secondary_text(
                    f"{err}.\n\nThe full, untrimmed recording is being saved "
                    f"instead — nothing was lost.")
                warn.run(); warn.destroy()
        self.win.destroy()
        self._on_save(chosen, src)

    def _discard(self):
        from gi.repository import Gtk
        dlg = Gtk.MessageDialog(transient_for=self.win, modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                text="Discard this recording?")
        dlg.format_secondary_text("The clip will be deleted and not saved.")
        resp = dlg.run(); dlg.destroy()
        if resp == Gtk.ResponseType.OK:
            self._stop_player()
            self.win.destroy()
            self._on_discard()

    def _on_close(self, *_):
        # Closing the window = discard, with a warning.
        self._discard()
        return True

    def present(self):
        """Raise the preview to the front (used when a new capture is attempted
        while this one is still waiting to be saved)."""
        try:
            self.win.present()
        except Exception:
            pass

    def run(self):
        self.win.show_all()
        self._playbin.set_state(Gst.State.PLAYING)
        # 100 ms keeps the playhead smooth without being noticeable work.
        self._poll_id = GLib.timeout_add(100, self._poll)


class RecordingSession:
    """Drives a recording end to end: portal handshake, encode to a temp file,
    a ● REC control card (timer + Stop/Cancel) placed off the recorded area,
    then a preview window to Save or Discard. run() returns the saved path or
    None."""

    def __init__(self, target, save_dir, region=None, monitors=None):
        import os
        import time
        self.target = target
        self.save_dir = save_dir
        self.region = region
        self.monitors = monitors or []
        os.makedirs(save_dir, exist_ok=True)
        stamp = time.strftime("CosmicShot_%Y-%m-%d_%H-%M-%S")
        self.final_path = os.path.join(save_dir, stamp + ".mp4")
        self._tmp = os.path.join(save_dir, "." + stamp + ".recording.mp4")
        self.recorder = None
        self.error = None
        self.saved = None
        self.audio_device = None
        self._elapsed = 0
        self._timer_id = None
        self._started = False
        self.ctrl = None
        self._timer_lbl = None
        self._dim = None
        self._panel_mode = False
        self._sig_id = None
        self._present_sig = None

    # -- control card ----------------------------------------------------
    def _build_control(self):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("GtkLayerShell", "0.1")
        from gi.repository import Gtk, GtkLayerShell

        win = Gtk.Window()
        GtkLayerShell.init_for_window(win)
        mon, anchors = self._place()
        if mon is not None:
            GtkLayerShell.set_monitor(win, mon)
        GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
        for edge, margin in anchors:
            GtkLayerShell.set_anchor(win, edge, True)
            GtkLayerShell.set_margin(win, edge, max(0, int(margin)))
        # ON_DEMAND (not EXCLUSIVE): the control takes the keyboard only while
        # it's focused, so panel menus / other windows can still be opened during
        # a recording. EXCLUSIVE held a global keyboard grab that dismissed any
        # menu the moment it tried to open.
        GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.ON_DEMAND)

        prov = Gtk.CssProvider()
        prov.load_from_data(
            b".rec-card{background-color:rgba(20,22,28,0.96);border:2px solid #ff4444;"
            b"border-radius:14px;padding:12px 18px;} .rec-card label{color:#fff;"
            b"font-size:15px;font-weight:bold;} .rec-card button{padding:4px 14px;"
            b"border-radius:9px;font-weight:bold;} .rec-stop{background-image:none;"
            b"background-color:#ff4444;color:#fff;}")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.get_style_context().add_class("rec-card")
        box.get_style_context().add_provider(prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win.add(box)
        self._timer_lbl = Gtk.Label(label="Choose what to record…")
        self._timer_lbl.get_style_context().add_provider(prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        box.pack_start(self._timer_lbl, False, False, 0)
        stop = Gtk.Button(label="Stop")
        stop.get_style_context().add_class("rec-stop")
        stop.get_style_context().add_provider(prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        stop.connect("clicked", lambda _b: self.stop())
        box.pack_start(stop, False, False, 0)
        cancel = Gtk.Button(label="Cancel (Esc)")
        cancel.connect("clicked", lambda _b: self.cancel())
        box.pack_start(cancel, False, False, 0)
        win.connect("key-press-event", self._on_key)
        self.ctrl = win
        win.show_all()

    def _place(self):
        """Return (gdk_monitor, [(Edge, margin), ...]) placing the control card
        OFF the recorded area: below the region, else above, else to the side;
        for screen recording, on a different monitor when there is one."""
        from gi.repository import Gdk, GtkLayerShell
        disp = Gdk.Display.get_default()
        E = GtkLayerShell.Edge

        def gdkmon(m):
            return disp.get_monitor(m.index)

        if self.target == "region" and self.region and self.monitors:
            rx, ry, rw, rh = self.region
            cx, cy = rx + rw / 2, ry + rh / 2
            host = next((m for m in self.monitors
                         if m.x <= cx < m.x + m.width and m.y <= cy < m.y + m.height),
                        self.monitors[0])
            CARD_H, CARD_W, GAP = 64, 320, 14
            below = (ry + rh - host.y) + GAP
            if below + CARD_H <= host.height:
                return gdkmon(host), [(E.TOP, below)]
            above = (ry - host.y) - GAP - CARD_H
            if above >= 8:
                return gdkmon(host), [(E.TOP, above)]
            # Region fills the height: put the card to whichever side has room,
            # vertically centred on the region.
            vmargin = max(8, min(int((ry - host.y) + rh / 2 - CARD_H / 2),
                                 host.height - CARD_H - 8))
            right_space = (host.x + host.width) - (rx + rw)
            left_space = rx - host.x
            if right_space >= CARD_W + GAP:
                return gdkmon(host), [(E.TOP, vmargin), (E.RIGHT, right_space - CARD_W - GAP)]
            if left_space >= CARD_W + GAP:
                return gdkmon(host), [(E.TOP, vmargin), (E.LEFT, left_space - CARD_W - GAP)]
            return gdkmon(host), [(E.BOTTOM, 8)]
        if self.target == "screen" and len(self.monitors) > 1:
            other = next((m for m in self.monitors if not m.primary), self.monitors[-1])
            return gdkmon(other), []
        return None, [(E.BOTTOM, 60)]

    def _on_key(self, _w, ev):
        from gi.repository import Gdk
        if ev.keyval in (Gdk.KEY_Escape, Gdk.KEY_q):
            self.cancel()
        return True

    # -- lifecycle -------------------------------------------------------
    def run(self):
        from gi.repository import Gtk
        from . import audio
        # Ask for an audio source up front (defaults to no sound). Cancel here
        # means don't record at all.
        proceed, self.audio_device = audio.choose_source()
        if not proceed:
            return None
        # Do NOT show the control card yet: it grabs the keyboard (EXCLUSIVE
        # layer-shell), and showing it now would sit on top of the portal's
        # consent dialog and block window/monitor selection. The portal dialog
        # is the only UI until the stream is granted (see _on_ready).
        stype = SOURCE_WINDOW if self.target == "window" else SOURCE_MONITOR
        self._portal = ScreenCastPortal(stype)
        self._portal.start(self._on_ready, self._on_error)
        Gtk.main()
        return self.saved

    def _on_ready(self, fd, node_id, props):
        fraction = self._crop_fraction() if (self.target == "region" and self.region) else None
        print(f"[record] stream ready: fd={fd} node={node_id} props={dict(props)} "
              f"crop_fraction={fraction} audio={self.audio_device}", file=sys.stderr, flush=True)
        try:
            self.recorder = Recorder(self._tmp)
            self.recorder.build(fd, node_id, audio_device=self.audio_device)
            if fraction:
                self.recorder.set_crop_fraction(fraction)
            self.recorder.play()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return self._on_error(str(exc))
        self._started = True
        # Always register the recording so it can be stopped by signal — from
        # the tray's red ⏹ button OR from `cosmicshot record --stop` (a hotkey,
        # for setups without a tray). If a tray is running, light up its panel
        # Stop button too. Region/app-window also keep the floating card off the
        # recorded area; full-screen relies on the panel/hotkey (a card would be
        # in the shot) and only falls back to a card when there's no tray.
        from . import lock
        self._register_recording()
        have_tray = bool(lock.tray_pid())
        if have_tray:
            self._signal_tray()
        if self.target != "screen" or not have_tray:
            if self.target == "region" and self.region:
                self._dim = _RegionDim(self.region, self.monitors)
            self._build_control()
        self._timer_id = GLib.timeout_add_seconds(1, self._tick)
        self._update()

    # -- stop-by-signal (tray button / `record --stop` hotkey) -----------
    def _register_recording(self):
        """Publish this recording's PID and listen for the stop signal."""
        import signal
        from . import lock
        self._panel_mode = True
        lock.write_recording_pid()
        self._sig_id = GLib.unix_signal_add(
            GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self._on_stop_signal)
        print("[record] recording registered (signal-stoppable)",
              file=sys.stderr, flush=True)

    def _on_stop_signal(self):
        self.stop()  # _close_overlays() -> _exit_panel_mode() removes this source
        return True

    def _signal_tray(self):
        """Nudge the tray to re-read the recording state (icon + menu)."""
        import os
        import signal
        from . import lock
        pid = lock.tray_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGUSR1)
            except OSError:
                pass

    def _exit_panel_mode(self):
        if not self._panel_mode:
            return
        from . import lock
        if self._sig_id:
            GLib.source_remove(self._sig_id); self._sig_id = None
        lock.clear_recording_pid()
        self._signal_tray()
        self._panel_mode = False

    def _crop_fraction(self):
        """Region as (fx, fy, fw, fh) fractions of its host monitor — invariant
        under display scaling, so it maps correctly onto the device-pixel frame
        the encoder actually receives."""
        rx, ry, rw, rh = self.region
        cx, cy = rx + rw / 2, ry + rh / 2
        host = next((m for m in self.monitors
                     if m.x <= cx < m.x + m.width and m.y <= cy < m.y + m.height),
                    self.monitors[0] if self.monitors else None)
        if host is None or host.width <= 0 or host.height <= 0:
            return None
        return ((rx - host.x) / host.width, (ry - host.y) / host.height,
                rw / host.width, rh / host.height)

    def _tick(self):
        self._elapsed += 1
        self._update()
        return True

    def _update(self):
        if not self._timer_lbl:
            return
        if self._started:
            m, s = divmod(self._elapsed, 60)
            self._timer_lbl.set_markup(
                f"<span foreground='#ff5555'>●</span> <b>REC  {m:02d}:{s:02d}</b>")
        else:
            self._timer_lbl.set_text("Choose what to record…")

    def _close_overlays(self):
        if self._timer_id:
            GLib.source_remove(self._timer_id); self._timer_id = None
        if self._dim is not None:
            self._dim.destroy(); self._dim = None
        if self.ctrl is not None:
            self.ctrl.destroy(); self.ctrl = None
        self._exit_panel_mode()

    def stop(self):
        import os
        print("[record] Stop clicked", file=sys.stderr, flush=True)
        self._close_overlays()
        if self.recorder is None:
            self.error = "Recording never started — no window or screen was selected."
            return self._quit()
        rec_err = getattr(self.recorder, "error", None)
        self.recorder.stop()
        size = os.path.getsize(self._tmp) if os.path.exists(self._tmp) else -1
        print(f"[record] after stop: tmp={self._tmp} exists={size >= 0} size={size} "
              f"rec_err={rec_err}", file=sys.stderr, flush=True)
        # A header-only file (no frames captured) is a few hundred bytes; treat
        # anything that small as an empty recording rather than show a broken
        # preview.
        if not os.path.exists(self._tmp) or os.path.getsize(self._tmp) < 1024:
            self.error = (f"Recording produced no video ({rec_err})" if rec_err
                          else "Recording produced no video (the capture stream sent no frames).")
            try:
                if os.path.exists(self._tmp):
                    os.unlink(self._tmp)
            except OSError:
                pass
            return self._quit()
        # Preview: let the user watch it, then Save (asking where) or Discard.
        from . import config
        cfg = config.load()
        start_dir = cfg.get("video_save_dir") or self.save_dir
        suggested = os.path.basename(self.final_path)
        try:
            prev = PreviewWindow(self._tmp, self._save_temp, self._discard_temp,
                                 suggested_name=suggested, start_dir=start_dir)
            # While the preview waits to be saved this process still holds the
            # capture lock, so a new capture attempt should raise this window
            # rather than do nothing — register for the standard present signal.
            import signal
            from . import lock
            lock.write_active_pid()
            self._present_sig = GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                lambda: (prev.present(), True)[1])
            prev.run()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            # Don't lose the recording if the player fails to build — move it to
            # its final path and report it as saved.
            try:
                os.replace(self._tmp, self.final_path)
                self.saved = self.final_path
            except OSError:
                self.saved = self._tmp
            self.error = f"Saved without preview (player error: {exc})"
            self._quit()

    def _save_temp(self, dest, src=None):
        """`src` is the temp recording, or the trimmed copy of it when the user
        trimmed in the preview — in which case the original is dropped."""
        import os
        import shutil
        src = src or self._tmp
        try:
            os.replace(src, dest)              # fast path: same filesystem
        except OSError:
            try:
                shutil.move(src, dest)         # cross-filesystem (different drive)
            except OSError:
                self.saved = src
                self.error = f"Could not save to {dest}"
                return self._quit()
        if src != self._tmp:
            try:
                os.unlink(self._tmp)
            except OSError:
                pass
        self.saved = dest
        # Remember the folder for next time.
        try:
            from . import config
            cfg = config.load()
            cfg["video_save_dir"] = os.path.dirname(os.path.abspath(dest))
            config.save(cfg)
        except Exception:
            pass
        self._quit()

    def _discard_temp(self):
        import os
        try:
            os.unlink(self._tmp)
        except OSError:
            pass
        self.saved = None
        self._quit()

    def cancel(self):
        import os
        self._close_overlays()
        if self.recorder:
            self.recorder.stop()
        try:
            if os.path.exists(self._tmp):
                os.unlink(self._tmp)
        except OSError:
            pass
        self.saved = None
        self._quit()

    def _on_error(self, msg):
        self.error = msg
        self._close_overlays()
        self._quit()

    def _quit(self):
        from gi.repository import Gtk
        from . import lock
        if self._present_sig:
            GLib.source_remove(self._present_sig); self._present_sig = None
        lock.clear_active_pid()
        GLib.idle_add(Gtk.main_quit)
