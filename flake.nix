{
  description = "kptncook - command line client for downloading KptnCook recipes";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);
      pkgsFor = system: import nixpkgs { inherit system; };

      # Runtime dependencies, taken from pyproject.toml [project.dependencies].
      pyDeps = ps: with ps; [
        httpx
        feedparser
        rich
        pydantic
        pydantic-settings
        typer
        click
        unidecode
        jinja2
        pathvalidate
      ];

      # Dev/test tooling available as Python packages in nixpkgs.
      pyDevDeps = ps: with ps; [
        pytest
        pytest-cov
        pytest-mock
        mypy
      ];

      kptncookFor = system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python313;
        in
        python.pkgs.buildPythonApplication {
          pname = "kptncook";
          version = "0.0.34";
          pyproject = true;

          src = ./.;

          # pyproject.toml pins uv_build<0.8.0, but nixpkgs ships a newer
          # uv-build; relax the upper bound so the wheel can be built.
          postPatch = ''
            substituteInPlace pyproject.toml \
              --replace-fail '"uv_build>=0.7.8,<0.8.0"' '"uv_build>=0.7.8"'
          '';

          build-system = [ python.pkgs.uv-build ];
          dependencies = pyDeps python.pkgs;

          # No runnable test entry points needed for the packaged app; the test
          # suite is available via the dev shell instead.
          doCheck = false;

          pythonImportsCheck = [ "kptncook" "kptncook_setup" ];

          meta = with pkgs.lib; {
            description = "Little command line utility to download KptnCook recipes";
            homepage = "https://github.com/ephes/kptncook";
            license = licenses.mit;
            mainProgram = "kptncook";
          };
        };
    in
    {
      packages = forAllSystems (system: {
        kptncook = kptncookFor system;
        default = kptncookFor system;
      });

      apps = forAllSystems (system:
        let kptncook = kptncookFor system; in {
          kptncook = {
            type = "app";
            program = "${kptncook}/bin/kptncook";
          };
          kptncook-setup = {
            type = "app";
            program = "${kptncook}/bin/kptncook-setup";
          };
          default = {
            type = "app";
            program = "${kptncook}/bin/kptncook";
          };
        });

      devShells = forAllSystems (system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python313;
          # A library build of the project, added to the dev env purely so that
          # `importlib.metadata.version("kptncook")` resolves (code still runs
          # live from ./src because PYTHONPATH takes precedence).
          kptncookLib = python.pkgs.buildPythonPackage {
            pname = "kptncook";
            version = "0.0.34";
            pyproject = true;
            src = ./.;
            postPatch = ''
              substituteInPlace pyproject.toml \
                --replace-fail '"uv_build>=0.7.8,<0.8.0"' '"uv_build>=0.7.8"'
            '';
            build-system = [ python.pkgs.uv-build ];
            dependencies = pyDeps python.pkgs;
            doCheck = false;
          };
          # Interpreter with runtime + dev deps so pytest/mypy import the
          # project straight from ./src (no build step, no uv needed).
          pythonEnv = python.withPackages (ps: pyDeps ps ++ pyDevDeps ps ++ [ kptncookLib ]);
          # Live-editable console entry points that run from ./src.
          kptncookDev = pkgs.writeShellScriptBin "kptncook" ''
            exec ${pythonEnv}/bin/python -m kptncook "$@"
          '';
          kptncookSetupDev = pkgs.writeShellScriptBin "kptncook-setup" ''
            exec ${pythonEnv}/bin/python -c \
              "import sys; from kptncook_setup import cli; sys.argv[0]='kptncook-setup'; cli()" "$@"
          '';
        in
        {
          default = pkgs.mkShell {
            # Wrappers first so the live ./src entry points win on PATH over the
            # console scripts from kptncookLib.
            packages = [
              kptncookDev
              kptncookSetupDev
              pythonEnv
              pkgs.ruff
              pkgs.just
            ];

            shellHook = ''
              # Import the project directly from source (live-editable).
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              # Keep kptncook's config/data (incl. .env) inside the project
              # folder instead of ~/.kptncook.
              export KPTNCOOK_HOME="''${KPTNCOOK_HOME:-$PWD}"
              echo "kptncook dev shell (Python ${python.version})"
              echo "  KPTNCOOK_HOME=$KPTNCOOK_HOME (.env read from here)"
              echo "  kptncook <cmd>     # runs live from ./src"
              echo "  pytest             # run tests"
              echo "  mypy src           # type check"
              echo "  ruff check .       # lint"
            '';
          };
        });
    };
}
