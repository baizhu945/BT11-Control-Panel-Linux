{ pkgs, ... }:

let
  python = pkgs.python3.withPackages (pythonPackages: [
    pythonPackages.tkinter
  ]);

  # The auto mode service reads PipeWire metadata only; no DSP dependency is needed.
  analysisPython = pkgs.python3;

  bt11Control = pkgs.runCommand "bt11-control" {
    nativeBuildInputs = [ pkgs.makeWrapper ];
  } ''
    install -Dm755 ${./bt11_control.py} $out/libexec/bt11-control.py
    makeWrapper ${python}/bin/python $out/bin/bt11-control \
      --add-flags "$out/libexec/bt11-control.py"
  '';

  # Watches PipeWire playback sample rates and picks aptX Adaptive mode 3/19.
  bt11AutoMode = pkgs.runCommand "bt11-auto-mode" {
    nativeBuildInputs = [ pkgs.makeWrapper ];
  } ''
    install -Dm755 ${./bt11_auto_mode.py} $out/libexec/bt11-auto-mode.py
    makeWrapper ${analysisPython}/bin/python $out/bin/bt11-auto-mode \
      --add-flags "$out/libexec/bt11-auto-mode.py" \
      --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.pipewire bt11Control ]}
  '';

  # The BT11's vendor HID interface.  The symlink only exists while the dongle
  # is plugged in, so it doubles as the presence test for the path unit.
  bt11Hidraw =
    "/dev/input/by-id/usb-FIIO_FIIO_BT11__UAC1.0_-if01-hidraw";
in
{
  home.packages = [ bt11Control bt11AutoMode ];

  # Nothing runs while the BT11 is unplugged: the path unit is the only piece
  # that stays loaded, and it starts the service as soon as the dongle appears.
  # The service exits by itself when the dongle is removed, which re-arms the
  # path unit for the next plug.
  systemd.user.paths.bt11-auto-mode = {
    Unit = {
      Description = "Start the BT11 auto mode service when the dongle appears";
    };

    Path = {
      PathExists = bt11Hidraw;
      Unit = "bt11-auto-mode.service";
    };

    Install.WantedBy = [ "default.target" ];
  };

  systemd.user.services.bt11-auto-mode = {
    Unit = {
      Description =
        "Match the FiiO BT11 aptX Adaptive mode to the playback sample rate";
      After = [ "pipewire.service" "wireplumber.service" ];
    };

    Service = {
      ExecStart = "${bt11AutoMode}/bin/bt11-auto-mode run";
      Restart = "on-failure";
      RestartSec = 5;
    };

    # Intentionally not enabled directly: the path unit owns the start.
  };
}

