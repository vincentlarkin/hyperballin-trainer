# Hyperballin Trainer

- Linux trainer for Hyperballin Alpha rc0209 under Steam/Proton.
- **Puremove:** subtle responsiveness and air-control improvements with natural momentum and braking.
- **Free move:** instant directional steering, stronger acceleration, and faster air movement.
- Movement/dash power, jump strength, gravity, and player-time controls.
- Health lock and instant ability cooldowns.
- Upward boost (**F6**), brake (**F7**), and restore defaults (**F8**).
- Live speed, health, coordinates, and position recording.
- Automatic save backups and player reattachment after respawns.
- Runtime overrides restore when disabled or the trainer closes.

## Compile on Linux

Requires an x86_64 Linux system, Git, and Podman. The build container installs the compiler dependencies.

```sh
git clone https://github.com/vincentlarkin/hyperballin-trainer.git
cd hyperballin-trainer
./build.sh
```

For Docker, use `CONTAINER_ENGINE=docker ./build.sh` instead.

The executable is `dist/hyperballin-trainer`. The release archive and `SHA256SUMS` are also written to `dist/`. Run the executable alongside the game on Linux with glibc 2.35 or newer and X11/XWayland.
