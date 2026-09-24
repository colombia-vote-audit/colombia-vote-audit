{
  description = "Colombia vote audit: fetch pipeline for congressional voting records";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: {
        default = pkgs.python3Packages.buildPythonApplication {
          pname = "cva";
          version = "0.1.0";
          pyproject = true;
          src = pkgs.lib.fileset.toSource {
            root = ./.;
            fileset = pkgs.lib.fileset.unions [
              ./pyproject.toml
              ./src
              ./tests
            ];
          };
          build-system = [ pkgs.python3Packages.hatchling ];
          dependencies = with pkgs.python3Packages; [
            httpx
            pypdf
          ];
          nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
        };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python3
            pkgs.uv
            pkgs.ruff
            pkgs.sqlite
            # pdftotext/pdfinfo for poking at gazettes by hand
            pkgs.poppler-utils
          ];
        };
      });

      nixosModules.default =
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          cfg = config.services.cva-pipeline;
        in
        {
          options.services.cva-pipeline = {
            enable = lib.mkEnableOption "the daily Colombia vote audit fetch pipeline";
            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
            };
            user = lib.mkOption {
              type = lib.types.str;
              description = "User the pipeline runs as; owns dataDir.";
            };
            dataDir = lib.mkOption {
              type = lib.types.path;
              description = "SQLite database and downloaded PDFs.";
            };
            onCalendar = lib.mkOption {
              type = lib.types.str;
              # Night in Colombia, when the upstream servers are quiet.
              default = "*-*-* 07:30:00 UTC";
            };
            billDetails = lib.mkOption {
              type = lib.types.int;
              default = 2000;
              description = "Max bill detail requests per run.";
            };
            workers = lib.mkOption {
              type = lib.types.int;
              default = 3;
              description = "Parallel archive sessions for PDF downloads.";
            };
            maxMinutes = lib.mkOption {
              type = lib.types.int;
              default = 600;
              description = "Stop starting new downloads after this long; the rest waits for the next run.";
            };
          };

          config = lib.mkIf cfg.enable {
            systemd.services.cva-pipeline = {
              after = [ "network-online.target" ];
              wants = [ "network-online.target" ];
              serviceConfig = {
                Type = "oneshot";
                User = cfg.user;
                ExecStart = lib.escapeShellArgs [
                  "${cfg.package}/bin/cva"
                  "--data-dir"
                  cfg.dataDir
                  "daily"
                  "--bill-details"
                  (toString cfg.billDetails)
                  "--workers"
                  (toString cfg.workers)
                  "--max-minutes"
                  (toString cfg.maxMinutes)
                ];
                # Bill details plus the download budget, with room to spare.
                # On stop, cva finishes in-flight downloads before exiting.
                TimeoutStartSec = "14h";
                TimeoutStopSec = "5min";
                Nice = 10;
              };
            };
            systemd.timers.cva-pipeline = {
              wantedBy = [ "timers.target" ];
              timerConfig = {
                OnCalendar = cfg.onCalendar;
                Persistent = true;
              };
            };
          };
        };
    };
}
