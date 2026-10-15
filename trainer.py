#!/usr/bin/env python3
"""Hyperballin Alpha rc0209: external trainer for the Linux/Proton build."""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import struct
import time
from dataclasses import dataclass

BUILD_SHA256 = "8ccf13c30b9fd32f70216a2e5e8dd4adc8e6869e2d399816c58660fe5e4bdc7c"
EXE_NAME = "project_garden-Win64-Shipping.exe"
NAME_BLOCKS_RVA = 0x96FBD50
OBJECT_ARRAY_RVA = 0x97C9D40
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "hyperballin-trainer"


def puremove_velocity(direction, velocity, minimum_speed):
    """Turn immediately toward input without discarding jump or dash momentum."""
    x, y = direction[:2]
    length = math.hypot(x, y)
    if length < 0.01:
        return None
    speed = max(math.hypot(*velocity[:2]), minimum_speed * min(length, 1.0))
    return (x / length * speed, y / length * speed, velocity[2])


class NotReady(RuntimeError):
    pass


@dataclass(frozen=True)
class ObjectRef:
    address: int
    index: int
    serial: int
    cls: int


@dataclass(frozen=True)
class Property:
    name: str
    kind: str
    offset: int
    size: int
    byte_offset: int = 0
    mask: int = 255


class Game:
    def __init__(self, writable=True):
        self.fd = -1
        self.pid = self.find_pid()
        maps = Path(f"/proc/{self.pid}/maps").read_text().splitlines()
        segments = [x.split(maxsplit=5) for x in maps]
        header = next((x for x in segments if len(x) == 6 and
                       x[5].endswith(EXE_NAME) and int(x[2], 16) == 0), None)
        if header is None:
            raise NotReady("Waiting for the game executable to load")
        self.base = int(header[0].split("-")[0], 16)
        self.exe = Path(header[5])
        with self.exe.open("rb") as f:
            sha = hashlib.sha256()
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha.update(chunk)
            digest = sha.hexdigest()
        if digest != BUILD_SHA256:
            raise NotReady("Unsupported game build. This trainer supports Alpha rc0209 only.")
        self.fd = os.open(f"/proc/{self.pid}/mem", os.O_RDWR if writable else os.O_RDONLY)
        self.names_address = self.base + NAME_BLOCKS_RVA
        self.array_address = self.base + OBJECT_ARRAY_RVA
        self.name_cache = {}
        self.properties_cache = {}
        self.patches = {}
        self.player = self.movement = None
        self.baseline = {}
        self.last_search = 0.0
        self.last_alive = 0.0
        self.backup_path = None
        self.puremove_airborne = False
        self.puremove_air_speed = 0.0
        if self.name(0) != "None":
            self.close()
            raise NotReady("Name table validation failed")
        self.object_table()
        if writable:
            self.backup_saves()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def find_pid():
        matches = []
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                if (p / "comm").read_text().strip() == "GameThread" and \
                        EXE_NAME.encode() in (p / "cmdline").read_bytes():
                    matches.append(int(p.name))
            except OSError:
                pass
        if len(matches) != 1:
            raise NotReady("Start Hyperballin in Steam" if not matches else
                           "More than one game instance is running")
        return matches[0]

    def read(self, address, size):
        if not 0x10000 <= address < 0x800000000000 or not 0 < size <= 2**24:
            raise NotReady("Invalid memory range")
        data = os.pread(self.fd, size, address)
        if len(data) != size:
            raise NotReady("Game memory changed; reconnecting")
        return data

    def unpack(self, fmt, address):
        return struct.unpack("<" + fmt, self.read(address, struct.calcsize("<" + fmt)))

    def u32(self, address):
        return self.unpack("I", address)[0]

    def u64(self, address):
        return self.unpack("Q", address)[0]

    def write(self, address, data):
        if os.pwrite(self.fd, data, address) != len(data):
            raise NotReady("Incomplete memory write")

    def name(self, index):
        if index not in self.name_cache:
            block_index, entry = index >> 16, index & 65535
            if block_index > self.u32(self.names_address - 8) or block_index > 8191:
                raise NotReady("Invalid name index")
            block = self.u64(self.names_address + block_index * 8)
            address = block + entry * 2
            header = self.unpack("H", address)[0]
            length, width = header >> 6, 1 + (header & 1)
            if not 0 < length < 1024:
                raise NotReady("Invalid name entry")
            self.name_cache[index] = self.read(address + 2, length * width).decode(
                "utf-16-le" if width == 2 else "utf-8", errors="replace")
        return self.name_cache[index]

    def object_name(self, address):
        return self.name(self.u32(address + 0x18))

    def object_table(self):
        table, count, maximum, chunks, max_chunks = self.unpack("QIIII", self.array_address)
        if not (1 <= chunks <= max_chunks <= 1024 and
                1000 <= count <= maximum <= 5000000 and
                chunks == (count + 65535) // 65536):
            raise NotReady("Object table validation failed")
        return table, count

    def item(self, index):
        table, count = self.object_table()
        if not 0 <= index < count:
            raise NotReady("Object no longer exists")
        chunk = self.u64(table + (index // 65536) * 8)
        data = self.read(chunk + (index % 65536) * 24, 24)
        return struct.unpack_from("<Q", data, 8)[0], struct.unpack_from("<I", data, 16)[0]

    def reference(self, address):
        data = self.read(address, 40)
        index = struct.unpack_from("<I", data, 12)[0]
        cls = struct.unpack_from("<Q", data, 16)[0]
        pointer, serial = self.item(index)
        if pointer != address:
            raise NotReady("Object identity changed")
        return ObjectRef(address, index, serial, cls)

    def valid(self, ref):
        if ref is None:
            return False
        try:
            pointer, serial = self.item(ref.index)
            return pointer == ref.address and serial == ref.serial and \
                self.u64(pointer + 0x10) == ref.cls and self.u32(pointer + 12) == ref.index
        except (OSError, NotReady, struct.error):
            return False

    def properties(self, cls):
        if cls in self.properties_cache:
            return self.properties_cache[cls]
        result, seen_classes = {}, set()
        original = cls
        while cls and cls not in seen_classes:
            seen_classes.add(cls)
            field = self.u64(cls + 0x50)
            seen_fields = set()
            while field and field not in seen_fields:
                if len(seen_fields) > 2048:
                    raise NotReady("Invalid property chain")
                seen_fields.add(field)
                data = self.read(field, 0x74)
                name = self.name(struct.unpack_from("<I", data, 0x20)[0])
                field_class = struct.unpack_from("<Q", data, 8)[0]
                kind = self.name(self.u32(field_class + 8))
                offset, size = struct.unpack_from("<I", data, 0x44)[0], \
                    struct.unpack_from("<I", data, 0x34)[0]
                if offset > 0x100000 or size > 0x100000:
                    raise NotReady("Invalid property layout")
                byte_offset, mask = (data[0x71], data[0x73]) if kind == "BoolProperty" else (0, 255)
                result.setdefault(name, Property(name, kind, offset, size, byte_offset, mask))
                field = struct.unpack_from("<Q", data, 0x18)[0]
            cls = self.u64(cls + 0x40)
        self.properties_cache[original] = result
        return result

    def prop(self, ref, name, expected=None):
        p = self.properties(ref.cls).get(name)
        if p is None or expected and p.kind != expected:
            raise NotReady(f"Unsupported property: {name}")
        return p

    def value(self, ref, name):
        if not self.valid(ref):
            raise NotReady("Player changed; waiting to reconnect")
        p = self.prop(ref, name)
        address = ref.address + p.offset
        fmt = {"FloatProperty": "f", "DoubleProperty": "d", "ByteProperty": "B",
               "IntProperty": "i", "ObjectProperty": "Q"}.get(p.kind)
        if fmt:
            value = self.unpack(fmt, address)[0]
            if isinstance(value, float) and not math.isfinite(value):
                raise NotReady("Invalid numeric property")
            return value
        if p.kind == "BoolProperty":
            return bool(self.read(address + p.byte_offset, 1)[0] & p.mask)
        if p.kind == "StructProperty" and p.size == 24:
            return self.unpack("ddd", address)
        raise NotReady(f"Unsupported value type: {name}")

    def set_value(self, ref, name, value, feature=None):
        if not self.valid(ref):
            raise NotReady("Player changed; write cancelled")
        p = self.prop(ref, name)
        address = ref.address + p.offset + (p.byte_offset if p.kind == "BoolProperty" else 0)
        fmt = {"FloatProperty": "f", "DoubleProperty": "d", "ByteProperty": "B",
               "IntProperty": "i"}.get(p.kind)
        if fmt:
            if not math.isfinite(value):
                raise ValueError("Value must be finite")
            data = struct.pack("<" + fmt, value)
        elif p.kind == "BoolProperty":
            old = self.read(address, 1)[0]
            data = bytes([(old | p.mask) if value else (old & ~p.mask)])
        elif p.kind == "StructProperty" and p.size == 24:
            if len(value) != 3 or not all(math.isfinite(v) and abs(v) <= 1e9 for v in value):
                raise ValueError("Invalid vector")
            data = struct.pack("<ddd", *value)
        else:
            raise NotReady(f"Writing {p.kind} is unsupported")
        if feature:
            key = (ref, name)
            if key not in self.patches:
                self.patches[key] = (feature, address, self.read(address, len(data)), p.mask if p.kind == "BoolProperty" else None)
        self.write(address, data)

    def restore(self, feature=None):
        errors = []
        for key, patch in list(self.patches.items()):
            group, address, original, mask = patch
            if feature is not None and feature != group:
                continue
            ref, _ = key
            try:
                if self.valid(ref):
                    if mask is not None:
                        current = self.read(address, 1)[0]
                        original = bytes([(current & ~mask) | (original[0] & mask)])
                    self.write(address, original)
            except (OSError, NotReady) as exc:
                errors.append(str(exc))
            finally:
                del self.patches[key]
        return errors

    def iter_objects(self):
        table, count = self.object_table()
        for ci in range((count + 65535) // 65536):
            chunk = self.u64(table + ci * 8)
            n = min(65536, count - ci * 65536)
            data = self.read(chunk, n * 24)
            for j in range(n):
                address = struct.unpack_from("<Q", data, j * 24 + 8)[0]
                if address:
                    yield address

    def ready(self):
        now = time.monotonic()
        if now - self.last_alive > 0.5:
            if self.find_pid() != self.pid:
                raise NotReady("Game restarted")
            self.last_alive = now
        if self.valid(self.player) and self.valid(self.movement):
            try:
                controller = self.reference(self.value(self.player, "Controller"))
                if self.value(controller, "Pawn") == self.player.address:
                    return True
            except (OSError, NotReady):
                pass
        self.restore()
        self.player = self.movement = None
        if now - self.last_search < 1.0:
            return False
        self.last_search = now
        player_class = None
        if hasattr(self, "player_class"):
            try:
                if self.object_name(self.player_class) == "BP_ThirdPersonCharacter_C":
                    player_class = self.player_class
            except (OSError, NotReady):
                pass
        if player_class is None:
            for obj in self.iter_objects():
                try:
                    if self.object_name(obj) == "BP_ThirdPersonCharacter_C" and \
                            self.object_name(self.u64(obj + 0x10)) == "BlueprintGeneratedClass":
                        player_class = self.player_class = obj
                        break
                except (OSError, NotReady, UnicodeError):
                    continue
        if player_class is None:
            return False
        for obj in self.iter_objects():
            try:
                if self.u64(obj + 0x10) != player_class or self.u32(obj + 8) & 0x10:
                    continue
                player = self.reference(obj)
                controller = self.reference(self.value(player, "Controller"))
                if self.object_name(controller.cls) != "PlayerController" or \
                        self.value(controller, "Pawn") != obj:
                    continue
                movement = self.reference(self.value(player, "CharacterMovement"))
                if self.object_name(movement.cls) != "CharacterMovementComponent":
                    continue
                self.player, self.movement = player, movement
                self.baseline = {}
                for group, ref, names in (
                    ("player", player, ("MarbleControlSpeed", "DashingSpeedBurst", "MarbleDashSpeedBurst",
                                        "SurfingSpeedBurst", "GravityScaleOG", "CustomTimeDilation")),
                    ("movement", movement, ("MaxAcceleration", "JumpZVelocity", "AirControl",
                                             "AirControlBoostMultiplier", "AirControlBoostVelocityThreshold",
                                             "FallingLateralFriction", "BrakingDecelerationFalling")),
                ):
                    for name in names:
                        self.baseline[(group, name)] = self.value(ref, name)
                self.puremove_airborne = False
                self.puremove_air_speed = 0.0
                return True
            except (OSError, NotReady, struct.error):
                continue
        return False

    def apply(self, speed=1.0, jump=1.0, gravity=1.0, tempo=1.0, health=False, cooldowns=False, puremove=False):
        if not self.ready():
            raise NotReady("Waiting for a playable character")
        if not puremove and abs(speed - 1.0) < 0.001:
            self.restore("speed")
        else:
            for group, name in (("movement", "MaxAcceleration"), ("player", "MarbleControlSpeed"),
                                ("player", "DashingSpeedBurst"), ("player", "MarbleDashSpeedBurst"),
                                ("player", "SurfingSpeedBurst")):
                ref = self.player if group == "player" else self.movement
                multiplier = speed * (8.0 if puremove and name == "MaxAcceleration" else 1.0)
                self.set_value(ref, name, self.baseline[(group, name)] * multiplier, "speed")
        for feature, multiplier, settings in (
            ("jump", jump, (("movement", "JumpZVelocity"),)),
            ("tempo", tempo, (("player", "CustomTimeDilation"),)),
        ):
            if abs(multiplier - 1.0) < 0.001:
                self.restore(feature)
            else:
                for group, name in settings:
                    ref = self.player if group == "player" else self.movement
                    self.set_value(ref, name, self.baseline[(group, name)] * multiplier, feature)
        if abs(gravity - 1.0) < 0.001:
            self.restore("gravity")
        else:
            scale = self.baseline[("player", "GravityScaleOG")] * gravity
            self.set_value(self.player, "GravityScaleOG", scale, "gravity")
            self.set_value(self.movement, "GravityScale", scale, "gravity")
        if health:
            self.set_value(self.player, "preventdamage", True, "health")
            maximum = self.value(self.player, "HealthOG")
            if not 0 < maximum < 1000000:
                raise NotReady("Unexpected health maximum")
            self.set_value(self.player, "Health", maximum)
        else:
            self.restore("health")
        if cooldowns:
            for name in ("CD_Airdash", "CD_Marbledash", "CD_Updash", "CD_Floorsurf"):
                self.set_value(self.player, name, -1.0)
        if puremove:
            for name, value in (("AirControl", max(4.0, self.baseline[("movement", "AirControl")])),
                                ("AirControlBoostMultiplier", 1.0), ("AirControlBoostVelocityThreshold", 0.0),
                                ("FallingLateralFriction", 0.0), ("BrakingDecelerationFalling", 0.0)):
                self.set_value(self.movement, name, value, "puremove")
            self.steer_puremove(speed)
        else:
            self.restore("puremove")
            self.puremove_airborne = False
            self.puremove_air_speed = 0.0

    def steer_puremove(self, speed):
        velocity = self.value(self.movement, "Velocity")
        airborne = self.value(self.movement, "MovementMode") == 3 or self.value(self.player, "AirTime") > 0.05
        if airborne and not self.puremove_airborne:
            self.puremove_air_speed = max(4000.0 * speed, math.hypot(*velocity[:2]) * 1.25)
        self.puremove_airborne = airborne
        minimum_speed = self.puremove_air_speed if airborne else 1800.0 * speed
        direction = self.value(self.player, "ControlInputVector")
        if math.hypot(*direction[:2]) < 0.01:
            if not self.value(self.player, "PressingMove"):
                return
            direction = self.value(self.player, "LastControlInputVector")
            if math.hypot(*direction[:2]) < 0.01:
                side, forward = self.value(self.player, "X Axis"), self.value(self.player, "Y Axis")
                controller = self.reference(self.value(self.player, "Controller"))
                _, yaw, _ = self.value(controller, "ControlRotation")
                yaw = math.radians(yaw)
                direction = (math.cos(yaw) * forward - math.sin(yaw) * side,
                             math.sin(yaw) * forward + math.cos(yaw) * side, 0.0)
        target = puremove_velocity(direction, velocity, minimum_speed)
        if target is not None:
            self.set_value(self.movement, "Velocity", target)

    def telemetry(self):
        if not self.ready():
            raise NotReady("Waiting for a playable character")
        velocity = self.value(self.movement, "Velocity")
        return dict(pid=self.pid, player_index=self.player.index,
                    position=self.value(self.movement, "LastUpdateLocation"),
                    velocity=velocity, speed=math.sqrt(sum(v * v for v in velocity)),
                    health=self.value(self.player, "Health"), max_health=self.value(self.player, "HealthOG"),
                    gravity=self.value(self.movement, "GravityScale"),
                    jump=self.value(self.movement, "JumpZVelocity"),
                    acceleration=self.value(self.movement, "MaxAcceleration"),
                    cooldowns={name: self.value(self.player, name) for name in
                               ("CD_Airdash", "CD_Marbledash", "CD_Updash", "CD_Floorsurf")})

    def boost(self, amount=4500.0):
        if not self.ready():
            raise NotReady("No playable character")
        x, y, _ = self.value(self.movement, "Velocity")
        self.set_value(self.movement, "PendingLaunchVelocity", (x, y, amount))

    def brake(self):
        if not self.ready():
            raise NotReady("No playable character")
        self.set_value(self.movement, "Velocity", (0.0, 0.0, 0.0))
        self.set_value(self.movement, "PendingLaunchVelocity", (0.0, 0.0, 0.0))

    def backup_saves(self):
        try:
            environ = Path(f"/proc/{self.pid}/environ").read_bytes().split(b"\0")
            compat = next((x.partition(b"=")[2].decode() for x in environ
                           if x.startswith(b"STEAM_COMPAT_DATA_PATH=")), None)
            if not compat:
                return
            saves = Path(compat) / "pfx/drive_c/users/steamuser/AppData/Local/project_garden/Saved/SaveGames"
            files = list(saves.glob("*.sav"))
            if not files:
                return
            destination = DATA_DIR / "backups" / time.strftime("%Y%m%d-%H%M%S")
            destination.mkdir(parents=True, exist_ok=True)
            import shutil
            for file in files:
                shutil.copy2(file, destination / file.name)
            self.backup_path = destination
        except OSError:
            raise NotReady("Could not back up the save; check the application data folder permissions")

    def close(self):
        if self.fd >= 0:
            try:
                if hasattr(self, "patches"):
                    self.restore()
            finally:
                os.close(self.fd)
                self.fd = -1


class KDEHotkeys:
    def __init__(self, callbacks):
        from gi.repository import Gio, GLib
        self.GLib, self.Gio = GLib, Gio
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.actions = []
        self.component = "hyperballin-trainer"
        self.callbacks = callbacks
        self.subscription = None
        try:
            for name, label, key in (("boost", "Boost upward", 0x01000035),
                                     ("brake", "Brake", 0x01000036),
                                     ("reset", "Restore defaults", 0x01000037)):
                action = [self.component, name, "Hyperballin Trainer", label]
                available = self.call("isGlobalShortcutAvailable", "(is)", (key, self.component)).unpack()[0]
                if not available:
                    raise RuntimeError("F6, F7 or F8 is already assigned")
                self.call("doRegister", "(as)", (action,))
                self.actions.append(action)
                actual = self.call("setShortcut", "(asaiu)", (action, [key], 6)).unpack()[0]
                if key not in actual:
                    raise RuntimeError("A shortcut could not be registered")
            path = self.call("getComponent", "(s)", (self.component,)).unpack()[0]
            self.subscription = self.bus.signal_subscribe(
                "org.kde.kglobalaccel", "org.kde.kglobalaccel.Component", "globalShortcutPressed",
                path, None, Gio.DBusSignalFlags.NONE, self.on_key)
        except Exception:
            self.close()
            raise

    def call(self, method, signature, args):
        return self.bus.call_sync("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel",
                                  method, self.GLib.Variant(signature, args), None,
                                  self.Gio.DBusCallFlags.NONE, 1500, None)

    def on_key(self, connection, sender, path, interface, signal_name, parameters):
        values = parameters.unpack()
        if values[0] == self.component and values[1] in self.callbacks:
            self.callbacks[values[1]]()

    def pump(self):
        context = self.GLib.MainContext.default()
        for _ in range(10):
            if not context.pending():
                break
            context.iteration(False)

    def close(self):
        if self.subscription is not None:
            self.bus.signal_unsubscribe(self.subscription)
            self.subscription = None
        for action in self.actions:
            try:
                self.call("unRegister", "(as)", (action,))
            except Exception:
                pass
        self.actions = []


def gui():
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Hyperballin Trainer")
    root.geometry("640x770")
    root.minsize(600, 755)
    style = ttk.Style(root)
    style.theme_use("clam")
    bg, panel, fg, muted, accent = "#111820", "#1c2733", "#eff5fb", "#a6b6c9", "#63dcdb"
    root.configure(bg=bg)
    style.configure(".", background=bg, foreground=fg, font=("Sans", 11))
    style.configure("TButton", background=panel, foreground=fg, padding=(12, 8))
    style.map("TButton", background=[("active", "#31475a")])
    style.configure("TCheckbutton", background=bg, foreground=fg)
    style.map("TCheckbutton", background=[("active", bg)])
    style.configure("TScale", background=bg, troughcolor=panel)
    style.configure("Title.TLabel", font=("Sans", 21, "bold"))
    style.configure("Muted.TLabel", foreground=muted)
    style.configure("Status.TLabel", foreground=accent)

    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Hyperballin", style="Title.TLabel").pack(anchor="w")
    ttk.Label(frame, text="Alpha rc0209 · Proton", style="Muted.TLabel").pack(anchor="w", pady=(2, 14))
    status = tk.StringVar(value="Looking for the game…")
    ttk.Label(frame, textvariable=status, style="Status.TLabel", wraplength=550).pack(anchor="w")
    telemetry = tk.StringVar(value="")
    ttk.Label(frame, textvariable=telemetry, style="Muted.TLabel", justify="left").pack(anchor="w", pady=(10, 14))
    variables = {}
    for name, title, low, high in (("speed", "Movement / dash power", .25, 5),
                                    ("jump", "Jump strength", .25, 5),
                                    ("gravity", "Gravity", 0, 2),
                                    ("tempo", "Player time", .25, 3)):
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=5)
        var = variables[name] = tk.DoubleVar(value=1)
        label = ttk.Label(row, text=title, width=25)
        label.pack(side="left")
        value_label = ttk.Label(row, text="1.00×", width=7, anchor="e")
        value_label.pack(side="right")
        def update(value, target=value_label):
            target.configure(text=f"{float(value):.2f}×")
        ttk.Scale(row, from_=low, to=high, variable=var, command=update).pack(side="left", fill="x", expand=True, padx=8)
        var.trace_add("write", lambda *_, v=var, target=value_label: target.configure(text=f"{v.get():.2f}×"))
    health, cooldowns, puremove = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar()
    ttk.Checkbutton(frame, text="Puremove", variable=puremove).pack(anchor="w", pady=(15, 5))
    ttk.Checkbutton(frame, text="Hold health full", variable=health).pack(anchor="w", pady=5)
    ttk.Checkbutton(frame, text="Instant ability cooldowns", variable=cooldowns).pack(anchor="w", pady=5)
    state = {"game": None, "closed": False, "last_attach": 0.0, "last_telemetry": 0.0, "event_until": 0.0}

    def message(text, seconds=3):
        status.set(text)
        state["event_until"] = time.monotonic() + seconds

    def action(name):
        game = state["game"]
        try:
            if game is None:
                raise NotReady("Start Hyperballin in Steam first")
            getattr(game, name)()
            message("Upward boost queued" if name == "boost" else "Velocity cleared")
        except (OSError, NotReady, ValueError) as exc:
            message(str(exc))

    def reset():
        for variable in variables.values():
            variable.set(1)
        health.set(False)
        cooldowns.set(False)
        puremove.set(False)
        if state["game"]:
            errors = state["game"].restore()
            message("Overrides restored" if not errors else "Some objects changed; restart the game to reset everything")
        else:
            message("All controls reset")

    def record():
        import csv
        from datetime import datetime
        try:
            if state["game"] is None:
                raise NotReady("Start Hyperballin in Steam first")
            t = state["game"].telemetry()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / "observations.csv"
            new_file = not path.exists()
            with path.open("a", newline="") as stream:
                writer = csv.writer(stream)
                if new_file:
                    writer.writerow(("time", "x_cm", "y_cm", "z_cm", "speed_cm_s", "health",
                                     "movement_multiplier", "jump_multiplier", "gravity_multiplier", "time_multiplier"))
                writer.writerow((datetime.now().astimezone().isoformat(timespec="seconds"),
                                 *t["position"], t["speed"], t["health"],
                                 *(round(variables[k].get(), 2) for k in ("speed", "jump", "gravity", "tempo"))))
            message("Position recorded in observations.csv")
        except (OSError, NotReady) as exc:
            message(str(exc))

    row = ttk.Frame(frame)
    row.pack(fill="x", pady=(16, 8))
    ttk.Button(row, text="Boost up · F6", command=lambda: action("boost")).pack(side="left", fill="x", expand=True, padx=(0, 6))
    ttk.Button(row, text="Brake · F7", command=lambda: action("brake")).pack(side="left", fill="x", expand=True, padx=(6, 0))
    ttk.Button(frame, text="Restore defaults · F8", command=reset).pack(fill="x", pady=5)
    ttk.Button(frame, text="Record position", command=record).pack(fill="x", pady=5)
    ttk.Label(frame, text="Zero gravity + repeated boosts can help explore high areas.\nCollision stays enabled. Close this window to restore overrides.",
              style="Muted.TLabel", wraplength=550, justify="left").pack(anchor="w", pady=(14, 5))
    hotkeys = None
    try:
        hotkeys = KDEHotkeys({"boost": lambda: action("boost"), "brake": lambda: action("brake"), "reset": reset})
        shortcut_text = "F6 / F7 / F8 work while the game is focused."
    except Exception as exc:
        shortcut_text = "Use the buttons; global shortcuts unavailable."
        print(f"Global shortcuts: {exc}", flush=True)
    ttk.Label(frame, text=shortcut_text, style="Muted.TLabel", wraplength=550).pack(anchor="w", pady=4)

    def close():
        if state["closed"]:
            return
        state["closed"] = True
        if state["game"]:
            state["game"].close()
        if hotkeys:
            hotkeys.close()
        root.destroy()

    def tick():
        if state["closed"]:
            return
        now = time.monotonic()
        if hotkeys:
            hotkeys.pump()
        try:
            game = state["game"]
            if game is None and now - state["last_attach"] >= 2:
                state["last_attach"] = now
                game = state["game"] = Game()
            if game:
                game.apply(**{name: round(var.get(), 2) for name, var in variables.items()},
                           health=health.get(), cooldowns=cooldowns.get(), puremove=puremove.get())
                if now > state["event_until"]:
                    status.set(f"Connected · PID {game.pid}")
                if now - state["last_telemetry"] >= .25:
                    state["last_telemetry"] = now
                    t = game.telemetry()
                    x, y, z = t["position"]
                    telemetry.set(f"Speed {t['speed']:,.0f} cm/s     Health {t['health']:.2f} / {t['max_health']:.2f}\n"
                                  f"X {x:,.0f}     Y {y:,.0f}     Z {z:,.0f} cm")
        except (OSError, NotReady, ValueError, struct.error) as exc:
            if now > state["event_until"]:
                status.set(str(exc))
            game = state["game"]
            if game and (not Path(f"/proc/{game.pid}").exists() or str(exc) == "Game restarted"):
                game.close()
                state["game"] = None
                telemetry.set("")
        root.after(8 if puremove.get() else 50, tick)

    root.protocol("WM_DELETE_WINDOW", close)
    atexit.register(lambda: state["game"].close() if state["game"] else None)
    signal.signal(signal.SIGTERM, lambda *_: close())
    signal.signal(signal.SIGINT, lambda *_: close())
    root.after(100, tick)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true", help="Print live player values without changing gameplay")
    args = parser.parse_args()
    if args.inspect:
        game = None
        try:
            game = Game(writable=False)
            print(json.dumps(game.telemetry(), indent=2))
        except (OSError, NotReady) as exc:
            parser.exit(1, f"{exc}\n")
        finally:
            if game:
                game.close()
    else:
        import fcntl
        lock_path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"hyperballin-trainer-{os.getuid()}.lock"
        with lock_path.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(1, "Hyperballin Trainer is already open.\n")
            gui()


if __name__ == "__main__":
    main()
