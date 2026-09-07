{ pkgs, ... }:

let
  python = pkgs.python3.withPackages (pythonPackages: [
    pythonPackages.tkinter
  ]);

  bt11Control = pkgs.runCommand "bt11-control" {
    nativeBuildInputs = [ pkgs.makeWrapper ];
  } ''
    install -Dm755 ${./bt11_control.py} $out/libexec/bt11-control.py
    makeWrapper ${python}/bin/python $out/bin/bt11-control \
      --add-flags "$out/libexec/bt11-control.py"
  '';
in
{
  home.packages = [ bt11Control ];
}
