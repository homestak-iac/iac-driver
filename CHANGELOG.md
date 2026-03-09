# Changelog

## Unreleased

## v0.56 - 2026-03-09

### Fixed
- Fix local-host detection when ssh_host is the machine's own IP address (#299)

### Changed
- Move PID files from `/var/run/homestak/` to `$HOMESTAK_ROOT/.run/`, remove sudo hack (#301)
- Move server repo checkouts from `/tmp/` to `$HOMESTAK_ROOT/.cache/server/repos/` (#302)
- Move tofu state from `iac-driver/.states/` to `$HOMESTAK_ROOT/.state/tofu/` (#304)
- Move TLS certs from `~/.homestak/tls/` to `$HOMESTAK_ROOT/config/tls/{hostname}.{crt,key}` (#289)
- Move config state from `config/.state/` to `$HOMESTAK_ROOT/.state/config/`, rename marker to `complete.json` (#303)
- Extract shared test doubles (MockHostConfig, TEST_SIGNING_KEY, mint_test_token) to conftest.py (#277)
- Remove redundant `sys.path.insert` from 27 test files (conftest.py handles it) (#277)

### Removed
- Remove legacy `inner_vm_id` and `test_vm_id` from HostConfig (#288)
- Remove `vm_id_attr` defaults from StartVMAction, WaitForGuestAgentAction, StartVMRemoteAction, WaitForGuestAgentRemoteAction (now required)

## v0.55 - 2026-03-08

No changes.

## v0.54 - 2026-03-08

### Changed
- Replace `HOMESTAK_SITE_CONFIG`, `HOMESTAK_LIB`, `HOMESTAK_ETC` with single `HOMESTAK_ROOT` anchor (#312)
  - `get_homestak_root()` replaces `get_homestak_lib()` and `get_homestak_etc()` in `common.py`
  - All paths derived: `$HOMESTAK_ROOT/config`, `$HOMESTAK_ROOT/iac/ansible`
  - `get_site_config_dir()`, `discover_etc_path()`, `discover_state_path()` simplified
  - Default: `$HOME` (on installed hosts, `$HOME` = workspace root)
- Update stale paths across codebase for multi-org migration (meta#320)
  - `site-config` → `config`, `~/lib/` → `~/iac/`, `~/etc/` → `~/config/`
  - `homestak-dev/packer` → `homestak-iac/packer` in config defaults
  - `install.sh` → `install` in comments and references

### Fixed
- Update SCP targets from `etc/` to `config/` for secrets and site.yaml (#292)
- Update homestak CLI path from `~/bin/homestak` to `~/bootstrap/homestak` (#293)
- Use `config/.state/` for runtime markers instead of `config/state/` (#291)
- Update SSH paths from `~/lib/iac-driver` to `~/iac/iac-driver` (#285)
- Fix server log path and repo discovery for multi-org layout
- Rename served repo from `site-config` to `config`

## v0.53 - 2026-03-06

### Changed
- Convert local PVE actions from SSH-to-self to subprocess (#267)
  - `StartVMAction`, `WaitForGuestAgentAction`, `LookupVMIPAction` use `run_command()` + `sudo`
  - Guest agent IP parsing uses Python `json.loads()` instead of `jq` shell pipeline
  - Remote variants (`StartVMRemoteAction`, `DiscoverVMsAction`) kept for delegation
- Add `sudo_prefix()` shared helper to `common.py` (#267)
- Enhance `ServerManager._is_local` to detect hostname match, not just loopback literals (#267)
- Replace hardcoded `user='root'` in `RemoveImageAction` with `automation_user` + sudo (#267)
- Use `sudo_prefix()` consistently in all file download/remove actions (#267)

### Fixed
- Set BootNext before reboot in pve-setup to prevent UEFI USB boot (#247)
- Use `automation_user` for server SSH in roundtrip scenarios (#279)
- Tighten `_is_ip_address()` to validate octet ranges (0-255), rejecting IPs like `999.999.999.999`

### Removed
- Remove `_run_remote()` paths from pve-setup scenario — local-first execution model only (#267)
  - Remove `_EnsurePVEPhase._run_remote()`, `_GenerateNodeConfigPhase._run_remote()`, `_CreateApiTokenPhase._run_remote()`
  - Remove `_wait_for_pvedaemon_remote()` and `_inject_token_remote()` helpers
  - Remove unused imports: `AnsiblePlaybookAction`, `EnsurePVEAction`, `run_ssh`, `wait_for_ssh`
- Remove `node_name` and `datastore` from `HostConfig` — only used by `ConfigResolver` (#275)
- Remove `ssh_user` from ConfigResolver tfvars output — tofu `var.ssh_user` was declared but never referenced (#275)
- Remove `pve_host_attr` from local action dataclasses — local actions no longer need host address (#267)

## v0.52 - 2026-03-02

### Changed
- **BREAKING**: Migrate from FHS paths to user-owned `~homestak/` model (bootstrap#75)
  - Add `get_homestak_lib()` and `get_homestak_etc()` helpers to `common.py`
  - Path discovery: `~/etc/` and `~/lib/` checked before FHS fallback
  - Remove `sudo` from file operations on user-owned paths (secrets, site config, SSH keys)
  - Delegation commands use `~/lib/iac-driver` and `./run.sh` (no sudo)
  - Server log moves from `/var/log/homestak/` to `~/log/`
  - State/spec paths use `~/etc/state/` instead of `/usr/local/etc/homestak/state/`

## v0.51 - 2026-02-28

### Added
- Add report generation to `manifest test` — writes JSON + Markdown reports to `reports/` with create/verify/destroy phase tracking (#226)
- Add `scripts/parallel-test.sh` — runs multiple manifest tests concurrently with a shared server (#203)
- `ServerManager` reads port from `config.spec_server` URL instead of hardcoded default (#203)
- Tofu state paths namespaced by manifest name (`.states/{manifest}/{node}-{host}/`) to avoid lock contention in parallel runs (#203)
- Report filenames include scenario slug for uniqueness in parallel runs (#203)

### Changed
- Rename `inner_ip` context key to `node_ip` across all actions, CLI (`--node-ip`), and tests — aligns with manifest vocabulary
- Rename `nested-pve` references to `child-pve` across defaults, comments, and test fixtures (ansible#49)
  - Default `pve_hostname` in `EnsurePVEAction`: `nested-pve` → `child-pve`
  - Default `name_pattern` in `DiscoverVMsAction`: `nested-pve*` → `child-pve*`

### Removed
- Remove `--skip-server` flag — replaced by `parallel-test.sh` external server management (#203)
- Remove deprecated `--remote` and `--vm-ip` CLI flags — use `-H <host>` instead (#235)
- Remove `--scenario` deprecation warning — `scenario run` verb is the primary interface (#235)
- Remove `RETIRED_SCENARIOS` dict and migration hints — no backward compatibility required (#235)

## v0.50 - 2026-02-22

### Theme: Provisioning Token (homestak-dev#231)

HMAC-SHA256 provisioning tokens replace posture-based auth for spec resolution.

### Added
- Wire `dns_servers` from site-config through ConfigResolver to tofu tfvars (iac-driver#229)
- Add `dns_servers` to `HostConfig` from site.yaml defaults (iac-driver#229)
- Include `dns-nameservers` in bridge config when `dns_servers` configured — fixes DNS loss on PVE nodes after bridge reconfig (iac-driver#229)
- Automate API token creation in `pve-setup` — creates pveum token, injects into secrets.yaml, verifies against PVE API (iac-driver#223)
- Auto-generate `auth.signing_key` during `pve-setup` when empty (#238)
- Add `./run.sh manifest validate` verb for FK validation against site-config (#207)
- Add push-mode config phase for leaf VMs in operator — resolves spec locally, runs ansible from controller targeting VM over SSH (#206)
- Auto-set `HOMESTAK_SOURCE` env var after server start so BootstrapAction and RecursiveScenarioAction use serve-repos instead of GitHub master (#189)

### Changed
- Default `run_ssh()` to current user via `getpass.getuser()` instead of hardcoded `root` (#251)
- Pass `config.ssh_user` explicitly in preflight packer image check (#251)
- Replace `[inner]` log prefix with delegate action name (e.g., `[delegate-root-pve]`) (#240)
- Suppress delegated JSON output from INFO logs — track brace depth for nested JSON (#240)
- Fix empty action name in delegate "Starting" message
- Strip ANSI escape codes from delegated error messages
- Downgrade auto-detected host and IP logs from INFO/WARNING to DEBUG/INFO
- Default to all SSH keys when spec omits `ssh_keys` — makes specs portable across deployments (#239)
- Remove obsolete `ssh_keys.` prefix handling in spec resolver (#239)
- Simplify `_image_to_asset_name()` — image names now map 1:1 to asset filenames (packer#48)
- Update default `packer_image` from `debian-12-custom.qcow2` to `debian-12.qcow2` (packer#48)
- Rewrite Makefile for venv-based dev tooling (PEP 668 compatibility) (#197)
  - `make install-dev` creates `.venv/`, installs linters + runtime deps
  - `make test` and `make lint` run via venv binaries
  - Pre-commit hooks (pylint, mypy) trigger on git commit

### Fixed
- Add preflight validation for empty `gateway` and `dns_servers` in site.yaml — fail early instead of cryptic DNS errors; warn (non-blocking) for empty `domain`
- Add preflight check for empty SSH keys in secrets.yaml — prevents 2-minute timeout with no explanation (#243)
- Add preflight check for missing packer images in PVE storage — local and remote via SSH (#243)
- Use `make init-secrets` fallback for missing secrets.yaml — handles both `.enc` decrypt and `.example` copy (#236)
- Handle inline empty dict (`api_tokens: {}`) in token injection (#237)
- Add `python3-requests` to `make install-deps` — required by `validation.py` (homestak-dev#266)
- Handle YAML null in `host-config.sh` output for empty `network.interfaces` (homestak-dev#266)
- Skip API preflight check for `pve-setup` scenario — PVE isn't installed yet on fresh hosts (homestak-dev#266)
- Handle local PVE install reboot — split into kernel/packages phases with idempotent re-entry via dpkg state detection (iac-driver#222)
- Fix root SSH failure in PVE lifecycle `post_scenario` phase — use `automation_user` instead of root, add sudo for `pve-setup --local` (#216)
- Reduce git dumb HTTP 404 noise — downgrade expected `/objects/` 404s to DEBUG log level (#205)
- Fix pull-mode spec_server in tiered PVE — use `HOMESTAK_SOURCE` env var so VMs reach the local server, not the outer host from site.yaml
- Fix push-mode config to run ansible from controller instead of inside VM (#206)
- Fix push-mode cloud-init race — skip spec injection for push-mode nodes so cloud-init doesn't bootstrap in parallel with operator config
- Fix pre-existing pylint/mypy warnings across cli.py, actions, config.py, executor.py (#209)
- Extract ServerManager from executor to bring module under 1000-line limit (#209)
- Remove implicit `DEFAULT_MANIFEST` fallback — all verbs now require explicit `-M` flag
- Restrict secrets.yaml to 600 permissions after SCP copy to bootstrapped hosts (#199)
  - CopySecretsAction now runs `chmod 600` + `chown root:root` after `sudo mv`
- Improve "secrets.yaml not found" error to suggest `make decrypt` when `.enc` exists (#202)
- Fix HOMESTAK_SOURCE propagation at depth 2+: use `--self-addr` from parent instead of localhost for server address; auto-detect external IP as fallback; override with `HOMESTAK_SELF_ADDR` env var (#200)
- Fix BootstrapAction TLS for serve-repos: add `-k` to curl and `HOMESTAK_INSECURE=1` for self-signed server certs (#189)
- Fix RepoManager to serve site-config from FHS path (`/usr/local/etc/homestak/`) via `extra_paths` (#189)

### Removed
- Remove dead `SyncDriverCodeAction` — serve-repos (`_working` branch) makes rsync-based code sync redundant (#212)
- Remove dead tfvars input path (`_load_from_tfvars`, `_parse_tfvars`) from config.py (#209)
- Remove fuser apt wait block from BootstrapAction — now handled by install.sh system-wide apt config (bootstrap#52, #198)

### Refactored
- Fix all mypy errors (40) and pylint warnings (97) — `make lint` now passes clean (#214)
- Extract `_handle_scenario_verb()`, `_resolve_host()`, `_setup_context()`, `_handle_results()` from 475-line `main()` in cli.py (#214)
- Split `test_actions.py` (27 tests) into per-module files: `test_actions_ssh.py`, `test_actions_file.py`, `test_actions_proxmox.py` (#214)
- Add unit tests for TofuApplyAction, TofuDestroyAction, AnsiblePlaybookAction, EnsurePVEAction (31 new tests, 579→610 total) (#215)
- Fix HTTP HEAD responses sending body which corrupted git clone persistent connections (#200)
- Fix `git commit-tree` failing on bootstrapped VMs with no git identity — set env vars for ephemeral commits in bare repo prep (#200)
- Fix SyncDriverCodeAction to also sync `run.sh` so inner PVE gets the `manifest` CLI verb (#189)
- Add `_mint_provisioning_token()` in ConfigResolver — mints HMAC-signed token at create time (#187)
- Add `verify_provisioning_token()` in server/auth — verifies HMAC, extracts spec FK from `s` claim (#187)
- Add `token inspect` CLI verb — decode/verify provisioning tokens (#187)
- Add `get_signing_key()` to ResolverBase (#187)

### Changed
- Server spec endpoint requires provisioning token; spec resolved from token's `s` claim, not URL identity (#187)
- Rename `HOMESTAK_SPEC_SERVER` → `HOMESTAK_SERVER` across all components (#188)
- Consolidate `HOMESTAK_IDENTITY` + `HOMESTAK_AUTH_TOKEN` into `HOMESTAK_TOKEN` (#187, #188)
- Cloud-init injects `HOMESTAK_SERVER` + `HOMESTAK_TOKEN` (was 3 env vars) (#187)
- `AuthError` now inherits from `Exception` (was dataclass) (#187)

### Removed
- Remove `validate_spec_auth()` — replaced by `verify_provisioning_token()` (#187)
- Remove `_resolve_auth_token()` and posture-based auth dispatch from ConfigResolver (#187)
- Remove `get_auth_method()` from SpecResolver (#187)
- Remove `get_auth_token()`, `get_site_token()`, `get_node_token()` from ResolverBase (#187)
- Remove `bootstrap-install` scenario — migrated to bootstrap repo as `tests/test-install-remote.sh` (bootstrap#45)
- Remove 5 packer-build scenarios and `--templates` flag (#195)
  - Deleted: packer-build, packer-build-publish, packer-build-fetch, packer-sync, packer-sync-build-fetch
  - Release workflow uses `gh release` commands; builds run directly via `packer/build.sh`

### Added
- Add preflight checks to manifest verb commands (apply/destroy/test) (#193)
  - Calls `validate_readiness()` before execution (API token, SSH, nested virt, lockfiles)
  - Add `--skip-preflight` flag to bypass checks
  - Derive `requires_nested_virt` from manifest topology (PVE nodes with children)
  - Skipped automatically for `--dry-run`

### Changed
- Restructure CLI from verb-first to noun-action pattern (#184)
  - `create/destroy/test` → `manifest apply/destroy/test`
  - `config --fetch` → `config fetch` + `config apply` (split into two commands)
  - `scenario <name>` → `scenario run <name>` (legacy syntax still supported)
  - Update subtree delegation and cloud-init runcmd for new syntax

### Testing
- Replace `10.0.12.x` test IPs with RFC 5737 TEST-NET-2 addresses (`198.51.100.x`) in 9 test files (#182)
- Add 9 unit tests for verb preflight integration (#193)

### Theme: Server Daemon Robustness (#177)

Proper daemonization replacing nohup/Popen hack with double-fork, PID files,
and health-check startup gate.

### Added
- Add `server` verb with `start`/`stop`/`status` subcommands (#177)
- Add `server/daemon.py` — double-fork daemonization, PID file management, health-check gate (#177)
- Add operator server lifecycle integration — auto start/stop around graph walk (#177)
- Add 31 unit tests for daemon.py (replaces 4 skipped server lifecycle tests)

### Changed
- Rename `src/controller/` → `src/server/` (#177)
- Simplify `run.sh` to 6-line exec wrapper (eliminates zombie wrapper process) (#177)
- Rewrite `StartServerAction`/`StopServerAction` to use `./run.sh server` CLI (#177)
- Replace `serve` verb with `server` verb in CLI (#177)

### Removed
- Remove `--serve-repos` flag and `start_serve_repos()`/`stop_serve_repos()` from run.sh (#177)
- Remove `scripts/serve-repos.sh` (#177)
- Remove `serve` verb from CLI (#177)

### Theme: Site-Config/IAC-Driver Cleanup, Pt.3 (#219)

### Changed
- `-H` flag now supports `user@host` syntax to override ssh_user (#179)
- `HostConfig.ssh_user` defaults to `$USER` instead of hardcoded `root` (#179)
- Rename scenarios: `spec-vm-push-roundtrip` → `push-vm-roundtrip`, `spec-vm-pull-roundtrip` → `pull-vm-roundtrip` (homestak-dev#214)
- Rename `spec_vm.py` → `vm_roundtrip.py` (homestak-dev#214)
- Update `DEFAULT_MANIFEST` from `n2-quick` to `n2-tiered` (homestak-dev#214)

### Removed
- Remove dead template mode code path from ConfigResolver and tofu actions (#180)
- Remove v1 manifest schema (levels-based): `ManifestLevel`, `_from_dict_v1()`, `_nodes_to_levels()` (#181)

### Theme: Site-Config/IAC-Driver Cleanup, Pt.2 (#212)

Complete v1→v2 config path migration and retire legacy entities.

### Docs
- Update CLAUDE.md: remove stale `resolve_env()`, `list_envs()`, `list_templates()` references; update resolution order, CLI options table (#211)

### Changed
- Migrate spec-vm scenarios from v1 env-based actions to inline actions (#173)
- Rename `TofuApplyInlineAction` → `TofuApplyAction`, `TofuDestroyInlineAction` → `TofuDestroyAction` (#173)
- Refactor `resolve_ansible_vars()` to accept posture name directly instead of env name (#173)
- `-H` now accepts raw IPs in addition to named hosts from site-config (#174)
- Deprecate `--remote` and `--vm-ip` flags with migration hint to use `-H` (#174)

### Removed
- Remove v1 `TofuApplyAction` (env-based) and `TofuDestroyAction` (env-based) (#173)
- Remove `resolve_env()` from ConfigResolver (#173)
- Remove `--env` flag and `list_envs()` — envs/ directory retired (site-config#58) (#174)
- Remove `list_envs()` and `list_templates()` from ConfigResolver (#174)

### Bug Fixes
- Fix stale controller detection in spec-vm scenarios — health check before declaring running (#176)

### Theme: Site-Config/IAC-Driver Cleanup (#209)

Unify resolver paths after site-config v2/ consolidation.

### Added
- Add `scenario` verb as first-class command: `./run.sh scenario pve-setup -H father` (#169)
- Add top-level usage display when running `./run.sh` with no arguments (#169)

### Changed
- Deprecate `--scenario` flag with migration hint (still works) (#169)
- Update ConfigResolver to load presets from `presets/` instead of `vms/presets/` (#161)
- Update ConfigResolver to use unified `postures/` (nested format) instead of dual v1/v2 loading (#161)
- Update `resolve_ansible_vars()` to read nested posture keys (`ssh.port`, `sudo.nopasswd`, etc.) (#161)
- Update SpecResolver to load specs from `specs/` instead of `v2/specs/` (#161)
- Update ResolverBase `_load_posture()` to use `postures/` instead of `v2/postures/` (#161)

### Fixed
- Fix controller startup 60s timeout caused by SSH FD inheritance (#166)
  - Close inherited FDs > 2 before exec'ing background controller process
  - Reduces `StartSpecServerAction` default timeout from 60s to 10s

### Removed
- Remove `v2_postures` secondary loader from ConfigResolver (#161)
- Remove vm- prefix stripping shim from manifest v2→v1 conversion (#161)

### Theme: Config Phase + Pull Execution Mode (#147)

Adds the config phase (`./run.sh config`) and pull execution mode for the node lifecycle. VMs can now self-configure via cloud-init instead of requiring SSH-based push from the driver.

### Added
- Add `config` verb (`./run.sh config`) for applying specs to the local host (#147)
  - Maps spec fields to ansible vars (packages, users, SSH keys, posture)
  - Runs `config-apply.yml` playbook with existing roles (base, users, security)
  - Writes platform-ready marker on success
  - Supports `--spec`, `--dry-run`, `--json-output` flags
- Add `WaitForFileAction` in `src/actions/ssh.py` (#147)
  - Polls for file existence on remote host via SSH
  - Used by operator to wait for pull-mode completion markers
- Add pull execution mode in operator (`manifest_opr/executor.py`) (#147)
  - Checks `execution.mode` per node after SSH becomes available
  - Pull nodes: polls for spec.yaml and config-complete.json markers
  - PVE nodes always use push (complex multi-step orchestration)
  - Push nodes: no change (default behavior preserved)
- Add `spec-vm-pull-roundtrip` integration test scenario (#156)
  - Validates autonomous spec fetch + config apply (pull mode end-to-end)
  - Verifies packages installed and users created after pull config
  - `VerifyPackagesAction` and `VerifyUserAction` reusable actions

### Theme: Integration Test (#198)

Validates all unreleased work since v0.45 on live PVE infrastructure. Fixes 7 bugs found during testing.

### Changed
- Update manifest references: drop `-v2` suffix from manifest names (site-config#51)
  - `n1-basic-v2` → `n1-basic`, `n2-quick-v2` → `n2-quick` in CLI, tests, docs
- Update StartSpecServerAction to use iac-driver controller instead of `homestak serve` (bootstrap#38)
  - Checks for `iac-driver/run.sh` instead of `homestak` CLI
  - Starts controller via `./run.sh serve` (HTTPS)
  - Logs to `/tmp/homestak-controller.log`

### Fixed
- Fix pull-mode bootstrap chain for controller-based repo serving (#163)
  - Fix `--repo-token ''` passthrough in controller CLI (empty string no longer triggers auto-generate)
  - Add `serve_repos` and `repo_token` parameters to `StartSpecServerAction`
  - Enable repo serving with disabled auth in `spec-vm-pull-roundtrip` scenario
  - Fix bare repo HEAD to point at `_working` branch (VMs were cloning `master` without uncommitted changes)
  - Set `ANSIBLE_CONFIG` explicitly in `config_apply.py` for cloud-init environments
  - Increase `wait_spec` timeout from 90s to 150s (bootstrap takes ~100s from boot)
- Fix operator context propagation after tofu apply (VM IDs not reaching downstream actions)
- Fix WaitForGuestAgentAction using wrong IP context key for named nodes
- Fix subtree delegation passing `--skip-preflight` (inner PVE needs preflight for lockfile cleanup)
- Fix RecursiveScenarioAction context extraction for verb command JSON format (nodes[] array)
- Add SyncDriverCodeAction to PVE lifecycle (ensures delegation uses same code as calling operator)
- Fix spec-vm-push-roundtrip server detection using pgrep (self-matches SSH commands)
- Fix spec-vm-push-roundtrip server stop using pgrep (replaced with port-based PID lookup)

---

### Theme: Scenario Consolidation (Phase 3 of #140)

Retires 9 legacy scenarios and 3 remote actions, replacing them with the manifest-based operator engine. PVE lifecycle and subtree delegation enable arbitrary nesting depth via verb commands.

### Added
- Add PVE lifecycle actions in `src/actions/pve_lifecycle.py` (#145)
  - 9 extracted actions: EnsureImageAction, BootstrapAction, CopySecretsAction, InjectSSHKeyAction, CopySSHPrivateKeyAction, InjectSelfSSHKeyAction, ConfigureNetworkBridgeAction, GenerateNodeConfigAction, CreateApiTokenAction
  - `_image_to_asset_name()` helper for manifest image → release asset mapping
- Add `ManifestGraph.extract_subtree()` for subtree delegation (#145)
  - Direct children promoted to roots; deeper descendants keep parent refs
  - Settings inherited from original manifest
- Add `raw_command` field to `RecursiveScenarioAction` (#145)
  - Enables verb delegation via SSH without legacy scenario wrappers
- Add operator PVE lifecycle and subtree delegation (#145)
  - `_run_pve_lifecycle()`: 10-phase PVE setup (bootstrap, secrets, bridge, API token, image download)
  - `_delegate_subtree()`: SSH to inner PVE, run `./run.sh create --manifest-json`
  - Arbitrary nesting depth (N=2, N=3, etc.) via recursion
- Add CLI migration hints for retired scenarios (#145)
  - `RETIRED_SCENARIOS` dict maps 9 old names to verb command equivalents
  - Running a retired scenario prints clear migration message + exit code 1

### Removed
- Remove legacy scenario files (#145): `vm.py`, `nested_pve.py`, `recursive_pve.py`, `cleanup_nested_pve.py`
  - Retired: vm-constructor, vm-destructor, vm-roundtrip, nested-pve-*, recursive-pve-*
- Remove legacy remote actions (#113): `TofuApplyRemoteAction`, `TofuDestroyRemoteAction`, `SyncReposToVMAction`
  - Replaced by RecursiveScenarioAction with raw_command and bootstrap-based installation

---

### Theme: Manifest-Based Orchestration Phase 2

Adds manifest schema v2 (graph-based nodes) and operator engine for create/destroy/test lifecycle.

### Added
- Add manifest schema v2 with graph-based `nodes[]` + `parent` references (#143)
  - `ManifestNode` dataclass with type, spec, preset, image, vmid, disk, parent
  - Graph validation: cycle detection, dangling parent refs, duplicate names
  - Topological sort converts v2 nodes to v1 levels for backward compatibility
  - `on_error` setting (stop, rollback, continue) in `ManifestSettings`

- Add operator engine package `manifest_opr/` (#144)
  - `graph.py` - `ExecutionNode` and `ManifestGraph` with create/destroy ordering
  - `state.py` - `NodeState` and `ExecutionState` with save/load to `.states/`
  - `executor.py` - `NodeExecutor` walks graph executing per-node lifecycle
  - `cli.py` - Verb CLI handlers for create/destroy/test

- Add verb commands: `create`, `destroy`, `test` (#144)
  - `./run.sh create -M <manifest> -H <host> [--dry-run] [--json-output]`
  - `./run.sh destroy -M <manifest> -H <host> [--dry-run] [--yes]`
  - `./run.sh test -M <manifest> -H <host> [--dry-run] [--json-output]`

- Add operator unit tests (#144)
  - `test_operator_graph.py` - Graph building, topo sort, ordering
  - `test_operator_state.py` - State save/load, node lifecycle transitions
  - `test_operator_executor.py` - Mocked action execution, error handling, dry-run

## Unified Controller Service

### Added
- Add unified controller daemon (`./run.sh serve`) (#148)
  - Single HTTPS server on port 44443 (configurable)
  - Serves both specs and git repos
  - Self-signed TLS certificate auto-generation with SHA256 fingerprint display
  - Graceful shutdown on SIGTERM/SIGINT, cache clear on SIGHUP

- Add spec serving endpoints (#148)
  - `GET /health` - Health check
  - `GET /specs` - List available specs
  - `GET /spec/{identity}` - Fetch resolved spec with FK resolution
  - Posture-based authentication (network, site_token, node_token)

- Add repo serving endpoints (#148)
  - `GET /{repo}.git/*` - Git dumb HTTP protocol (objects, refs, info)
  - `GET /{repo}.git/{path}` - Raw file extraction via `git show`
  - Bearer token authentication for all repo endpoints
  - `_working` branch contains uncommitted changes snapshot

- Add resolver modules (#148)
  - `resolver/base.py` - Shared FK resolution utilities
  - `resolver/spec_resolver.py` - Migrated from bootstrap with FK resolution
  - `resolver/spec_client.py` - HTTP client for spec fetching

- Add controller modules (#148)
  - `controller/tls.py` - TLS certificate generation and management
  - `controller/auth.py` - Authentication middleware (spec postures + repo tokens)
  - `controller/specs.py` - Spec endpoint handler
  - `controller/repos.py` - Repo endpoint handler with bare repo preparation
  - `controller/server.py` - Unified HTTPS server with routing
  - `controller/cli.py` - CLI integration for serve verb

- Add comprehensive unit tests (149 tests) (#148)
  - test_resolver_base.py - FK resolution utilities
  - test_spec_resolver.py - Spec loading and resolution
  - test_ctrl_tls.py - TLS certificate management
  - test_ctrl_auth.py - Authentication middleware
  - test_ctrl_specs.py - Spec endpoint handler
  - test_ctrl_repos.py - Repo endpoint handler
  - test_ctrl_server.py - Unified server with integration tests

### Changed
- Migrate SpecResolver from bootstrap to iac-driver (#139)
  - Now uses shared FK resolution from `resolver/base.py`
  - Error classes moved to `resolver/base.py` for reuse

## v0.45 - 2026-02-02

### Theme: Create Integration

Integrates create phase with config mechanism for automatic spec discovery on first boot.

### Added
- Add `spec-vm-push-roundtrip` scenario for Create → Specify validation (#154)
  - Verifies spec_server env vars injected via cloud-init
  - Tests VM connectivity to spec server
  - Full roundtrip: provision → verify → destroy
- Add `spec_server` to ConfigResolver output for Create → Specify flow (#154)
  - Reads from `site.yaml` defaults.spec_server
  - Included in tfvars.json for tofu cloud-init injection
- Add per-VM `auth_token` resolution based on posture (#154)
  - Loads v2/postures for `auth.method` (network, site_token, node_token)
  - Resolves tokens from `secrets.yaml` auth section
  - Added to each VM in vms[] list for cloud-init injection
- Add `posture` parameter to `resolve_inline_vm()` for manifest-driven scenarios

### Changed
- Add serve command availability check to `StartSpecServerAction` (#154)
  - Verifies `homestak serve` exists before attempting to start
  - Provides clear error message with upgrade instructions for older installations

## v0.44 - 2026-02-02

- Release alignment with homestak v0.44

## v0.43 - 2026-02-01

- Release alignment with homestak v0.43

## v0.42 - 2026-01-31

- Release alignment with homestak v0.42

## v0.41 - 2026-01-31

### Added
- Add vm_preset mode to manifest schema (#135)
  - Levels can now use `vm_preset` + `vmid` + `image` instead of `env` FK
  - Decouples manifests from envs/ - simpler configuration
  - ConfigResolver resolves vm_preset directly from vms/presets/

- Add `LookupVMIPAction` for destructor IP resolution
  - Queries PVE guest agent for VM IP when context doesn't have it
  - Enables destructor to find inner host without context file
  - Falls back gracefully when VM is stopped or unreachable

- Add `CopySSHPrivateKeyAction` for recursive PVE scenarios (#133)
  - Copies outer host's SSH private key to inner host
  - Enables inner-pve to SSH to its nested VMs
  - Keys copied to both root and homestak users
  - Required for n3-full (3-level nesting) to work

- Add serve-repos propagation to `RecursiveScenarioAction` (#134)
  - Passes HOMESTAK_SOURCE, HOMESTAK_TOKEN, HOMESTAK_REF to inner hosts
  - Enables nested bootstrap operations to use serve-repos instead of GitHub
  - Required for testing uncommitted code at level 2+ in recursive scenarios

- Add n3-full validation to release process (#130)
  - 3-level nested PVE now validated before releases
  - Proves recursive architecture scales beyond N=2

### Fixed
- Fix SSH authentication for automation_user in recursive scenarios (#133)
  - All SSH actions now use `config.automation_user` (homestak) for VM connections
  - `config.ssh_user` (root) reserved for PVE host connections
  - Fixes "Permission denied (publickey)" errors in nested deployments

- Fix API token injection in n3-full recursive scenarios (#130)
  - `CreateApiTokenAction` now uses level name as token key (was hardcoded to 'nested-pve')
  - Token injection now adds new entries if key doesn't exist (was replace-only)
  - Fixes test-vm provisioning failure at level 3

- Fix sudo env var passing in SSH commands
  - Use `sudo env VAR=value command` instead of `VAR=value sudo command`
  - Ensures environment variables reach the command on remote hosts

## v0.40 - 2026-01-29

### Added
- Add provider lockfile validation to preflight checks (#122)
  - Detects when cached lockfiles in `.states/*/data/` have stale provider versions
  - Auto-fixes by deleting stale lockfiles (regenerated on next `tofu init`)
  - Prevents "does not match configured version constraint" errors after Dependabot updates
  - New functions: `parse_provider_version()`, `parse_lockfile_version()`, `validate_provider_lockfiles()`

- Add split file handling to `DownloadGitHubReleaseAction` (#123)
  - Automatically detects and downloads split parts (`.partaa`, `.partab`, etc.)
  - Reassembles parts into single file after download
  - Cleans up part files after successful reassembly
  - Enables downloading large images (>2GB) from GitHub releases

## v0.39 - 2026-01-22

### Added
- Add `RecursiveScenarioAction` for SSH-streamed scenario execution (#104)
  - PTY allocation for real-time output streaming
  - JSON result parsing from `--json-output` scenarios
  - Context key extraction for parent scenario consumption
  - Configurable timeout and SSH user

- Add manifest-driven recursive scenarios (#114)
  - `manifest.py` with `Manifest`, `ManifestLevel`, `ManifestSettings` dataclasses
  - `ManifestLoader` for YAML file loading from site-config/manifests/
  - Schema versioning (v1 = linear levels array)
  - Depth limiting via `--depth` flag
  - JSON serialization for recursive calls via `--manifest-json`

- Add recursive-pve scenarios for N-level nested PVE (#114)
  - `recursive-pve-constructor`: Build N-level stack per manifest
  - `recursive-pve-destructor`: Tear down stack in reverse order
  - `recursive-pve-roundtrip`: Constructor + destructor full cycle
  - Helper actions: `BootstrapAction`, `CopySecretsAction`, `GenerateNodeConfigAction`

- Add CLI flags for manifest-driven scenarios
  - `--manifest`, `-M`: Manifest name from site-config/manifests/
  - `--manifest-file`: Path to manifest file
  - `--manifest-json`: Inline manifest JSON (for recursive calls)
  - `--keep-on-failure`: Keep levels on failure for debugging
  - `--depth`: Limit manifest to first N levels

- Add raw file serving to serve-repos.sh for fully offline bootstrap (#119)
  - `serve_raw_file()` extracts files from bare repos via `git show`
  - BootstrapAction fetches `{source_url}/bootstrap.git/install.sh` when HOMESTAK_SOURCE set
  - Enables recursive scenarios without GitHub connectivity

### Fixed
- Fix `BootstrapAction` to integrate with serve-repos env vars (#116)
  - Reads `HOMESTAK_SOURCE`, `HOMESTAK_TOKEN`, `HOMESTAK_REF` from environment
  - Builds bootstrap command with proper env var prefix for dev workflow
  - Falls back to GitHub URL when serve-repos not configured

- Fix `timeout_buffer` manifest setting not applied to recursive timeouts (#117)
  - Add `_get_recursive_timeout()` method to `RecursivePVEBase`
  - Subtracts `timeout_buffer` from base timeout to ensure cleanup time
  - Applies to all `RecursiveScenarioAction` invocations

- Fix `cleanup_on_failure` manifest setting not propagated (#118)
  - Add `_get_effective_keep_on_failure()` method to `RecursivePVEBase`
  - CLI `--keep-on-failure` takes precedence over manifest setting
  - Manifest `cleanup_on_failure: false` maps to `keep_on_failure: true`
  - Setting propagated to recursive constructor calls

### Testing
- Add unit tests for RecursiveScenarioAction (27 tests)
- Add unit tests for manifest loading and validation (29 tests)

## v0.38 - 2026-01-21

### Added
- Add `--json-output` flag for structured scenario results (#109)
  - JSON output to stdout, logs to stderr
  - Includes scenario name, success status, duration, phase results
  - Context values (vm_ip, vm_id, etc.) included for parent consumption
  - Error details included on failure

## v0.37 - 2026-01-20

### Theme: Foundation for Recursion

### Added
- Add HTTP server helper for dev workflows (iac-driver#110)
  - `scripts/serve-repos.sh` creates bare repos with `_working` branch containing uncommitted changes
  - Bearer token authentication via custom Python HTTP handler
  - OS-assigned ports by default with `--json` output for programmatic use
  - Automatic cleanup on exit (trap EXIT)

- Add `--serve-repos` flag to run.sh for HTTP server lifecycle management (iac-driver#110)
  - `--serve-repos` starts serve-repos.sh before scenario, stops on exit
  - `--serve-port` for explicit port (default: OS-assigned)
  - `--serve-timeout` for auto-shutdown
  - `--serve-ref` for ref selection (default: `_working`)
  - Exports `HOMESTAK_SOURCE`, `HOMESTAK_TOKEN`, `HOMESTAK_REF` for scenarios

## v0.36 - 2026-01-20

### Theme: Host Provisioning Workflow

### Added
- Host resolution fallback for pre-PVE hosts (#66)
  - `--host X` now checks `nodes/X.yaml` first, falls back to `hosts/X.yaml`
  - Enables provisioning fresh Debian hosts before PVE is installed
  - `list_hosts()` returns combined list from both directories (deduplicated)
  - `HostConfig.is_host_only` flag indicates SSH-only config (no PVE API)
  - Improved error message with instructions for creating host config

- Add `generate_node_config` phase to pve-setup scenario (#66)
  - Automatically generates `nodes/{hostname}.yaml` after PVE install
  - Local mode: runs `make node-config FORCE=1` in site-config
  - Remote mode: generates on target, copies back via scp
  - Host becomes usable for vm-constructor immediately after pve-setup

### Documentation
- Add "Host Resolution (v0.36+)" section to CLAUDE.md
- Update pve-setup scenario description (now 3 phases)

## v0.33 - 2026-01-19

### Theme: Unit Testing

### Added
- Add pytest job to CI workflow (#106)
  - Tests run on push/PR to master
  - 165 tests validated

### Fixed
- Fix Makefile test target to run from correct directory (#106)

## v0.32 - 2026-01-19

### Added
- Add `--version` to run.sh/cli.py using git-derived version pattern (#102)
- Add `--help` to helper scripts (setup-tools.sh, wait-for-guest-agent.sh) (#102)

### Fixed
- Fix GitHub org in setup-tools.sh (`john-derose` → `homestak-dev`) (#102)
- Add site-config to repos cloned by setup-tools.sh (#102)

## v0.31 - 2026-01-19

### Added
- Expand pytest coverage (#98)
  - Add tests/test_config.py for config discovery (get_site_config_dir, list_hosts, load_host_config)
  - Add tests/test_common.py for utilities (run_command, run_ssh, wait_for_ping, wait_for_ssh)

### Changed
- Make vmid_range configurable in NestedPVEDestructor (#101)
  - Add `vmid_range` class attribute (default: 99800-99999)
  - Can be overridden via subclass or at runtime

## v0.30 - 2026-01-18

### Fixed
- Use unique temp files for tfvars to avoid permission issues
  - Add `create_temp_tfvars()` helper using Python tempfile module
  - Clean up temp files after tofu commands complete
  - Remote actions use PID-based unique filenames

## v0.28 - 2026-01-18

### Features

- Add VM discovery actions for pattern-based cleanup (#41)
  - `DiscoverVMsAction`: Query PVE API and filter by name pattern + vmid range
  - `DestroyDiscoveredVMsAction`: Stop and destroy all discovered VMs
  - `DestroyRemoteVMAction`: Best-effort cleanup on remote PVE (handles missing host gracefully)
  - Destructor no longer requires context file with VM IDs

- Update NestedPVEConstructor to use granular playbooks (#49)
  - `setup_network`: Configure vmbr0 bridge via nested-pve-network.yml
  - `setup_ssh`: Copy SSH keys via nested-pve-ssh.yml
  - `setup_repos`: Sync repos and configure PVE via nested-pve-repos.yml
  - Better phase-level visibility and easier debugging

- Update NestedPVEDestructor to use discovery-based cleanup (#41)
  - Discovers VMs matching `nested-pve*` pattern in vmid range 99800-99999
  - Works without context file - just specify `--host`
  - Gracefully skips inner PVE cleanup when not reachable

### Fixed

- Fix DownloadGitHubReleaseAction to resolve 'latest' tag via GitHub API
  - GitHub download URLs require actual tag names, not 'latest'
  - New `_resolve_latest_tag()` method queries API for real tag name
  - Enables `packer_release: latest` in site-config to work correctly

## v0.26 - 2026-01-17

- Release alignment with homestak v0.26

## v0.25 - 2026-01-16

- Release alignment with homestak v0.25

## v0.24 - 2026-01-16

### Added
- Add comprehensive preflight checks (#97)
  - Bootstrap installation validation (checks for core repos)
  - site-init completion check (secrets.yaml decrypted, node config exists)
  - Nested virtualization check (for nested-pve-* scenarios)
  - Standalone `--preflight` mode for checking without scenario execution
  - `--skip-preflight` flag to bypass checks for experienced users
  - Clear, actionable error messages with remediation hints

### Changed
- Update site-config discovery to support FHS-compliant paths (#97)
  - Add `/usr/local/etc/homestak/` as priority 3 in resolution order
  - Legacy `/opt/homestak/site-config/` remains as fallback (priority 4)

## v0.22 - 2026-01-15

### Changed

- Refactor nested-pve scenario to pass `homestak_src_dir` instead of individual repo paths
  - Aligns with ansible#13 role refactor
  - Simplifies variable passing to ansible playbooks

## v0.20 - 2026-01-14

### Changed

- Refactored packer scenarios to use build.sh wrapper
  - PackerBuildAction now runs `./build.sh <template>` instead of direct packer commands
  - Ensures version detection, renaming, and cleanup scripts run during scenario builds
  - Increased timeout to 900s to accommodate PVE image builds

## v0.19 - 2026-01-14

### Features

- Add API token validation via `--validate-only` flag (#31)
  - Validates API token without running scenario
  - Reports PVE version on success
- Add host availability check with SSH reachability test (#32)
  - Pre-flight validation includes SSH connectivity
  - Fails fast before scenario execution
- Enhance `--local` flag with auto-config from hostname (#26)
  - Auto-discovers node config from system hostname
  - Simplifies local execution without explicit host parameter

### Fixed

- Fix EnsurePVEAction to detect pre-installed debian-13-pve image
  - Checks for `/etc/pve-packages-preinstalled` marker before pveproxy status
  - Skips ansible pve-install.yml when using pre-built PVE image

## v0.18 - 2026-01-13

### Features

- Add `--dry-run` mode for scenario preview (#40)
  - Shows phases, actions, and parameters without execution
  - Useful for release verification and understanding scenario behavior
  - Orchestrator returns preview report without side effects

## v0.17 - 2026-01-11

### Features
- Add site-config integration to ansible actions (#92)
  - `use_site_config` parameter to enable ConfigResolver integration
  - `env` parameter to specify environment for posture resolution
  - Resolves timezone, packages, SSH settings from site-config
  - Works with both `AnsiblePlaybookAction` and `AnsibleLocalPlaybookAction`

## v0.16 - 2026-01-11

### Features

- Add `--vm-id` CLI flag for ad-hoc VM ID overrides (closes #18)
  - Repeatable: `--vm-id test=99990 --vm-id inner=99912`
  - Format validation with clear error messages
  - Applied via `vm_id_overrides` in TofuApplyAction

### Code Quality

- Align CI workflow with local pre-commit configuration (closes #71)
  - Run `pre-commit run --all-files` in CI (advisory mode)
  - Replaces standalone pylint/mypy steps
  - Consistent tooling between local dev and CI

### Refactoring

- Remove redundant timeout overrides in nested-pve scenarios (closes #44)
  - Delete 7 overrides that matched action defaults
  - Keep 2 VerifySSHChainAction timeouts with rationale comments
  - Reduces maintenance burden and improves readability

### Testing

- Add unit tests for `--vm-id` CLI flag (5 tests):
  - Flag acceptance, format validation, edge cases
- Add unit test for TofuApplyAction VM ID override logic

## v0.13 - 2026-01-10

### Features

- Add `resolve_ansible_vars()` to ConfigResolver
  - Resolves site-config postures to ansible-compatible variables
  - Merges packages from site.yaml + posture (deduplicated)
  - Outputs timezone, SSH settings, sudo, fail2ban config
- Add `readiness.py` module with pre-flight checks:
  - `validate_api_token()` - Test PVE API token before tofu runs
  - `validate_host_available()` - Check host reachability via SSH
  - `validate_host_resolvable()` - Verify DNS resolution

### Testing

- Add `conftest.py` with shared pytest fixtures
  - `site_config_dir` - Temporary site-config structure
  - `mock_config_resolver` - Pre-configured resolver for tests
- Add tests for `resolve_ansible_vars()` (posture loading, package merging)
- Add tests for readiness checks (API validation, host checks)

### Code Quality

- Add `.pre-commit-config.yaml` for pylint/mypy hooks
- Add `mypy.ini` configuration
- Update Makefile with `lint` and `install-hooks` targets

### Documentation

- Document `resolve_ansible_vars()` in CLAUDE.md
- Add ansible output structure example

## v0.12 - 2025-01-09

- Release alignment with homestak-dev v0.12

## v0.11 - 2026-01-08

### Code Quality

- Improve pylint score from 8.31 to 9.58/10
- Fix all mypy type errors (8 → 0)
- Add `.pylintrc` configuration with project-specific rules
- Add encoding parameter to all `open()` calls
- Add explicit `check=False` to `subprocess.run()` calls
- Fix unused variables by using `_` convention
- Remove unused imports across action modules

### Refactoring

- Rename `test_ip` context key to `leaf_ip` for generalized naming (closes #68)
  - Better describes position in nesting hierarchy (leaf = innermost VM)
  - Prepares for v0.20 recursive nested PVE architecture

### Security

- Add confirmation prompt for destructive scenarios (closes #65)
  - `vm-destructor` and `nested-pve-destructor` now require confirmation
  - New `--yes`/`-y` flag to skip prompt (for automation/CI)
  - Destructive scenarios have `requires_confirmation = True` attribute

### Testing

- Add ConfigResolver test suite (16 tests):
  - IP validation (CIDR format, dhcp, None, bare IP rejection)
  - VM resolution with preset/template inheritance
  - vmid allocation (base + index, explicit override)
  - tfvars.json generation
  - list_envs/templates/presets methods
- Add action test suite (11 tests):
  - SSHCommandAction success/failure/missing context
  - WaitForSSHAction with mocked SSH
  - StartVMAction with mocked Proxmox
  - ActionResult dataclass defaults
- Add confirmation requirement tests (4 tests)
- Total tests: 30 → 61 (+31 tests)

### Documentation

- Update CLAUDE.md with `--yes`/`-y` flag
- Update context key documentation (`test_ip` → `leaf_ip`)

## v0.10 - 2026-01-08

### Documentation

- Update terminology: E2E → integration testing throughout
- Fix CLAUDE.md: correct CLI help text (pve-configure → pve-setup)

### CI/CD

- Add GitHub Actions workflow for pylint

### Housekeeping

- Enable secret scanning and Dependabot

## v0.9 - 2026-01-07

### Features

- Add scenario annotations for declarative behavior (closes #58, #60)
  - `requires_root`: Scenarios can declare root requirement for `--local` mode
  - `requires_host_config`: Scenarios can opt out of requiring `--host`
  - `expected_runtime`: Runtime estimates shown in `--list-scenarios`
- Add `--timeout`/`-t` flag for scenario-level timeout (closes #33)
  - Checks elapsed time before each phase
  - Fails gracefully if timeout exceeded (does not interrupt running phases)
- Add host auto-detection for `--local` mode (closes #58)
  - When `--local` specified without `--host`, detects from hostname
  - Works when hostname matches a configured node name
- Add runtime estimates to `--list-scenarios` output (closes #33)
  - Shows `~Nm` or `~Ns` format next to each scenario
  - All 14 scenarios now have `expected_runtime` attribute

### Changes

- Scenarios without host config requirement no longer need `--host`:
  - `pve-setup`, `user-setup`: Can auto-detect or use `--remote`
  - All packer scenarios: Work with `--local` or `--remote`
- Orchestrator now logs total scenario duration on completion

### Testing

- Add unit test suite (`tests/`) with 30 tests covering:
  - Scenario attributes (requires_root, requires_host_config, expected_runtime)
  - CLI integration (auto-detect, timeout flag, list-scenarios)
  - All scenarios have required attributes

### Documentation

- Update CLAUDE.md Available Scenarios table with Runtime column
- Add `--timeout` to CLI Options table
- Document scenario annotation system in Protocol docstring

## v0.8 - 2026-01-06

### Features

- Add `--context-file`/`-C` flag for scenario chaining (closes #38)
  - Persist VM IDs and IPs between constructor/destructor runs
  - Enables split workflows without `--inner-ip` workarounds
- Add `--packer-release` flag for image version override (closes #39)
  - Defaults to `latest` tag (maintained by packer release process)
  - Override with specific version: `--packer-release v0.7`
- Add CIDR validation for static IP configuration (closes #35)
  - ConfigResolver validates IPs use CIDR notation (e.g., `10.0.12.100/24`)
  - Catches misconfiguration before tofu/cloud-init errors

### Changes

- Remove default `--host` value (closes #36)
  - CLI now requires explicit `--host` for scenarios that need it
  - Prevents accidental operations on wrong host
- Harmonize and reduce timeout defaults (closes #34)
  - `TofuApplyAction.timeout_apply`: 600s → 300s
  - `wait_for_ssh()`: 300s → 60s
  - `WaitForSSHAction.timeout`: 120s → 60s
  - SSH waits now consistent across actions

### Bug Fixes

- Fix context loss between constructor and destructor runs (closes #37)
  - Resolved by `--context-file` feature

### Documentation

- Add Timeout Configuration section to CLAUDE.md
- Document `--context-file` usage patterns
- Document packer release resolution order

## v0.7 - 2026-01-06

### Features

- Pass `gateway` through ConfigResolver for static IP configurations (closes #30)

### Changes

- Move state storage from `tofu/.states/` to `iac-driver/.states/` (closes #29)
  - State now lives alongside the orchestrator that manages it
  - Each env+node gets isolated state: `.states/{env}-{node}/terraform.tfstate`
- Update docs: replace deprecated `pve` with real node names (`father`, `mother`)

### Documentation

- Fix state storage path in CLAUDE.md

## v0.6 - 2026-01-06

### Phase 5: ConfigResolver

- Add `ConfigResolver` class for site-config YAML resolution
- Resolves vms/presets, vms/templates, envs with inheritance
- vmid auto-allocation: vmid_base + index (or null for PVE auto-assign)
- Generates flat tfvars.json for tofu (replaces config-loader)
- Integrate ConfigResolver into tofu actions:
  - `TofuApplyAction` / `TofuDestroyAction` - local execution with ConfigResolver
  - `TofuApplyRemoteAction` / `TofuDestroyRemoteAction` - recursive pattern via SSH
- State isolation via explicit `-state` flag per env+node
- Update scenarios to use `env_name` instead of `env_path`
- Pass VM IDs from tofu actions to context for downstream actions
- Update proxmox actions to check context first, then config (dynamic VMID support)
- Add `env` parameter to `run_command()` for environment variable passthrough
- Add `--env`/`-E` CLI flag to override scenario environment
- Add `StartProvisionedVMsAction` and `WaitForProvisionedVMsAction` for multi-VM environments
- TofuApplyAction now adds `provisioned_vms` list to context for downstream actions
- E2E validated with nested-pve-roundtrip on father (~8.5 min)
- Tested `ansible-test` environment with Debian 12 + 13 VMs on father

### Housekeeping

- Rename `pve-deb` to `nested-pve` across codebase (closes #13)
- Update documentation for ansible collection structure (closes #19)
  - Roles now in `homestak.debian` and `homestak.proxmox` collections
  - Playbooks use FQCN (e.g., `homestak.debian.iac_tools`)

### Bug Fixes

- **OpenTofu state version 4 workaround**: Separate `TF_DATA_DIR` (data/) from state file location to avoid legacy code path that rejects v4 states. See [opentofu/opentofu#3643](https://github.com/opentofu/opentofu/issues/3643)
- **rsync fallback**: `SyncReposToVMAction` now uses tar pipe when rsync unavailable on target
- **VM ID context passing**: `TofuApplyRemoteAction` resolves config locally to extract VM IDs for downstream actions

### Packer Build Scenarios (closes #25)

- Add `packer-build` scenarios for remote image builds
  - `packer-build`: Build images locally or remotely
  - `packer-build-fetch`: Build on remote, fetch to local (for releases)
  - `packer-build-publish`: Build and publish to PVE storage
  - `packer-sync`: Sync local packer repo to remote
  - `packer-sync-build-fetch`: Dev workflow (sync, build, fetch)
- Add `--templates` CLI flag for building specific templates
- Add `--local`/`--remote` support for packer scenarios
- Prerequisites: Remote host must be bootstrapped with `homestak install packer`

## v0.5.0-rc1 - 2026-01-04

Consolidated pre-release with full YAML configuration support.

### Highlights

- YAML configuration via site-config (nodes/*.yaml, secrets.yaml)
- config-loader integration with tofu
- Full E2E validation with nested-pve-roundtrip

### Changes

- Fix ansible.posix.synchronize for /opt/homestak path
- Fix API token creation idempotency in nested-pve role
- Reduce polling intervals for faster E2E tests

## v0.4.0 - 2026-01-04

### Features

- YAML configuration support via site-config
  - Node config from `site-config/nodes/*.yaml`
  - Secrets from `site-config/secrets.yaml` (resolved by key reference)
  - Configuration merge order: site.yaml → nodes/{node}.yaml → secrets.yaml
- Pass `node` and `site_config_path` vars to tofu for config-loader module

### Changes

- Switch from tfvars to YAML configuration parsing

## v0.3.0 - 2026-01-04

### Features

- Add `pve-configure` scenario for PVE host configuration (runs pve-setup.yml + user.yml)
- Add `AnsibleLocalPlaybookAction` for local playbook execution
- Add `--local` and `--remote` CLI flags for execution mode
- Add configurable `ssh_user` for non-root SSH access (with sudo)

### Changes

- **BREAKING**: Move secrets to [site-config](https://github.com/homestak-dev/site-config) repository
- Host discovery now reads from `site-config/nodes/*.yaml`
- Remove in-repo SOPS encryption (Makefile, .githooks, .sops.yaml)

## v0.1.0-rc1 - 2026-01-03

### Features

- Modular scenario architecture with reusable actions
- Actions: tofu, ansible, ssh, proxmox, file operations
- Scenarios: simple-vm-*, nested-pve-* (constructor/destructor/roundtrip)
- CLI with --scenario, --host, --skip, --list-scenarios, --list-phases
- JSON + Markdown test report generation
- Auto-discovery of hosts from secrets/*.tfvars

### Infrastructure

- Branch protection enabled (PR reviews for non-admins)
- Dependabot for dependency updates
- secrets-check workflow for encrypted credentials
