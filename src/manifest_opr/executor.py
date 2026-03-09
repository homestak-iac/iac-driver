"""Node executor for manifest-based orchestration.

Walks the execution graph and runs per-node lifecycle operations.
Root nodes (depth 0) execute locally; children of PVE nodes are
delegated via SSH to the PVE host.
"""

import logging
import shlex
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from common import ActionResult, run_command, run_ssh
from config import HostConfig, get_sibling_dir
from manifest import Manifest
from manifest_opr.graph import ExecutionNode, ManifestGraph
from manifest_opr.server_mgmt import ServerManager
from manifest_opr.state import ExecutionState

logger = logging.getLogger(__name__)


@runtime_checkable
class ActionRunner(Protocol):
    """Protocol for action classes that implement run()."""

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Execute the action."""


@dataclass
class NodeExecutor:
    """Executes lifecycle operations on manifest graph nodes.

    Walks the graph in topological order, running create/destroy/test
    operations for each node using existing action classes.

    Only root nodes (depth 0) are handled locally. Children of PVE nodes
    are delegated via SSH to the PVE host using RecursiveScenarioAction.

    Attributes:
        manifest: The v2 manifest defining the deployment
        graph: The execution graph built from the manifest
        config: Host configuration for the target PVE host
        dry_run: If True, preview operations without executing
        json_output: If True, emit structured JSON
    """
    manifest: Manifest
    graph: ManifestGraph
    config: HostConfig
    dry_run: bool = False
    json_output: bool = False
    self_addr: Optional[str] = None
    _server: ServerManager = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Initialize the server manager after dataclass init."""
        spec_server = getattr(self.config, 'spec_server', '') or ''
        self._server = ServerManager(
            ssh_host=self.config.ssh_host,
            ssh_user=self.config.automation_user,
            self_addr=self.self_addr,
            port=ServerManager.resolve_port(spec_server),
        )

    def create(self, context: dict) -> tuple[bool, ExecutionState]:
        """Execute create lifecycle: provision root nodes, delegate subtrees."""
        state = ExecutionState(self.manifest.name, self.config.name)
        state.start()

        # Register all nodes
        for exec_node in self.graph.create_order():
            state.add_node(exec_node.name)

        if self.dry_run:
            self._preview_create()
            state.finish()
            return True, state

        # Ensure server is running for spec serving (pull mode, etc.)
        self._server.ensure()

        on_error = self.manifest.settings.on_error
        created_nodes: list[ExecutionNode] = []
        success = True

        try:
            for exec_node in self.graph.create_order():
                # Only handle root nodes locally; children are delegated
                if exec_node.depth > 0:
                    continue

                node_state = state.get_node(exec_node.name)
                node_state.start()

                result = self._create_node(exec_node, context)
                if not result.success:
                    node_state.fail(result.message)
                    success = False
                    logger.error("Create failed for node '%s': %s",
                                 exec_node.name, result.message)
                    if on_error == 'stop':
                        break
                    if on_error == 'rollback':
                        self._rollback(created_nodes, context, state)
                        break
                    continue  # on_error == 'continue'

                context.update(result.context_updates or {})
                vm_id = result.context_updates.get(f'{exec_node.name}_vm_id')
                ip = result.context_updates.get(f'{exec_node.name}_ip')
                node_state.complete(vm_id=vm_id, ip=ip)
                created_nodes.append(exec_node)
                state.save()

                # If PVE node with children: delegate subtree
                if exec_node.manifest_node.type == 'pve' and exec_node.children:
                    ok = self._handle_subtree_delegation(
                        exec_node, context, state)
                    if not ok:
                        success = False
                        if on_error == 'stop':
                            break
                        if on_error == 'rollback':
                            self._rollback(created_nodes, context, state)
                            break
        finally:
            self._server.stop()

        state.finish()
        state.save()
        return success, state

    def destroy(self, context: dict) -> tuple[bool, ExecutionState]:
        """Execute destroy lifecycle: delegate subtree destruction, then destroy roots."""
        # Try to load existing state for IPs/IDs
        state = self._load_or_create_state()
        state.start()

        # Merge state context into context (so destroy can find IPs)
        context.update(state.to_context())

        if self.dry_run:
            self._preview_destroy()
            state.finish()
            return True, state

        # Ensure server is running (needed for subtree delegation)
        self._server.ensure()

        success = True

        try:
            # Process root nodes only; children are delegated
            for exec_node in self.graph.destroy_order():
                if exec_node.depth > 0:
                    continue

                # If PVE node with children: delegate subtree destruction
                if exec_node.manifest_node.type == 'pve' and exec_node.children:
                    if not self._handle_subtree_destroy(
                            exec_node, context, state):
                        success = False

                # Now destroy the root node itself
                ns = state.get_node(exec_node.name) if exec_node.name in state.nodes else state.add_node(exec_node.name)
                ns.start()

                result = self._destroy_node(exec_node, context)
                if result.success:
                    ns.mark_destroyed()
                else:
                    ns.fail(result.message)
                    success = False
                    logger.error("Destroy failed for node '%s': %s",
                                 exec_node.name, result.message)
        finally:
            self._server.stop()

        state.finish()
        state.save()
        return success, state

    def test(self, context: dict) -> tuple[bool, ExecutionState]:
        """Execute test lifecycle: create, verify, destroy.
        """
        if not self.dry_run:
            self._server.ensure()

        try:
            # Create (server already running, create() will see it as healthy and reuse)
            create_ok, state = self.create(context)
            if not create_ok:
                if self.manifest.settings.cleanup_on_failure:
                    logger.info("Create failed, cleaning up...")
                    self.destroy(context)
                return False, state

            # Verify SSH on all created nodes
            verify_ok = self._verify_nodes(context, state)

            # Destroy (server still running, destroy() will reuse)
            destroy_ok, _ = self.destroy(context)

            return create_ok and verify_ok and destroy_ok, state
        finally:
            if not self.dry_run:
                self._server.stop()

    def _create_node(self, exec_node: ExecutionNode, context: dict) -> ActionResult:
        """Create a single node: provision, start, wait for IP/SSH, PVE lifecycle.

        For PVE-type nodes, runs the full PVE lifecycle after SSH is available:
        bootstrap, copy secrets, inject SSH keys, pve-setup, configure bridge,
        generate node config, create API token, inject self SSH key, download images.

        Returns ActionResult with context_updates containing {name}_vm_id and {name}_ip.
        """
        from actions.tofu import TofuApplyAction
        from actions.proxmox import StartVMAction, WaitForGuestAgentAction
        from actions.ssh import WaitForSSHAction

        mn = exec_node.manifest_node
        start = time.time()

        # Determine PVE host for this node
        if exec_node.is_root:
            pve_host = self.config.ssh_host
        else:
            parent_ip = context.get(f'{exec_node.parent.name}_ip')
            if not parent_ip:
                return ActionResult(
                    success=False,
                    message=f"Parent '{exec_node.parent.name}' IP not in context",
                    duration=time.time() - start,
                )
            pve_host = parent_ip

        logger.info(f"[create] Provisioning node '{mn.name}' on {pve_host}")

        # 1. Tofu apply
        # Only pass spec for pull-mode nodes — spec triggers cloud-init
        # bootstrap (HOMESTAK_SERVER/HOMESTAK_TOKEN injection). Push-mode
        # nodes get config from the operator, not from cloud-init.
        exec_mode = mn.execution_mode or self.manifest.execution_mode
        tofu_spec = mn.spec if exec_mode == 'pull' else None
        apply_action = TofuApplyAction(
            name=f'provision-{mn.name}',
            vm_name=mn.name,
            vmid=mn.vmid,
            vm_preset=mn.preset,
            image=mn.image,
            spec=tofu_spec,
            manifest_name=self.manifest.name,
        )
        result = apply_action.run(self.config, context)
        if not result.success:
            return result

        context_updates = dict(result.context_updates or {})
        context.update(context_updates)

        # 2. Start VM
        start_action = StartVMAction(
            name=f'start-{mn.name}',
            vm_id_attr=f'{mn.name}_vm_id',
        )
        start_result = start_action.run(self.config, context)
        if not start_result.success:
            return ActionResult(
                success=False,
                message=f"Start VM failed for {mn.name}: {start_result.message}",
                duration=time.time() - start,
                context_updates=context_updates,
            )

        # 3. Wait for guest agent / IP
        wait_action = WaitForGuestAgentAction(
            name=f'wait-ip-{mn.name}',
            vm_id_attr=f'{mn.name}_vm_id',
            ip_context_key=f'{mn.name}_ip',
            timeout=300,
        )
        wait_result = wait_action.run(self.config, context)
        if not wait_result.success:
            return ActionResult(
                success=False,
                message=f"Wait for IP failed for {mn.name}: {wait_result.message}",
                duration=time.time() - start,
                context_updates=context_updates,
            )

        context.update(wait_result.context_updates or {})
        context_updates.update(wait_result.context_updates or {})

        # Extract IP
        ip = context.get(f'{mn.name}_ip') or context.get('vm_ip')
        if ip:
            context_updates[f'{mn.name}_ip'] = ip

        # 4. Wait for SSH
        if self.manifest.settings.verify_ssh and ip:
            # Ensure IP is in context under the key WaitForSSHAction expects
            context[f'{mn.name}_ip'] = ip
            ssh_action = WaitForSSHAction(
                name=f'wait-ssh-{mn.name}',
                host_key=f'{mn.name}_ip',
                timeout=120,
            )
            ssh_result = ssh_action.run(self.config, context)
            if not ssh_result.success:
                return ActionResult(
                    success=False,
                    message=f"SSH wait failed for {mn.name}: {ssh_result.message}",
                    duration=time.time() - start,
                    context_updates=context_updates,
                )

        # 5. Post-SSH: PVE lifecycle, pull mode wait, or pass-through
        exec_mode = mn.execution_mode or self.manifest.execution_mode
        if mn.type == 'pve' and ip:
            # PVE lifecycle requires push: bootstrap install, secrets injection,
            # bridge config, API token creation, and image download are
            # multi-step orchestration steps that need the driver's active
            # participation. A single spec→ansible flow can't cover these.
            pve_result = self._run_pve_lifecycle(exec_node, ip, context)
            if not pve_result.success:
                return ActionResult(
                    success=False,
                    message=f"PVE lifecycle failed for {mn.name}: {pve_result.message}",
                    duration=time.time() - start,
                    context_updates=context_updates,
                )
        elif exec_mode == 'pull' and ip:
            # Pull mode: VM self-configures, poll for completion markers
            pull_result = self._wait_for_config_complete(exec_node, ip, context)
            if not pull_result.success:
                return ActionResult(
                    success=False,
                    message=f"Pull mode wait failed for {mn.name}: {pull_result.message}",
                    duration=time.time() - start,
                    context_updates=context_updates,
                )
        elif exec_mode == 'push' and ip and mn.type != 'pve' and mn.spec:
            # Push mode: driver pushes resolved spec, triggers config apply
            push_result = self._push_config(exec_node, ip, context)
            if not push_result.success:
                return ActionResult(
                    success=False,
                    message=f"Push config failed for {mn.name}: {push_result.message}",
                    duration=time.time() - start,
                    context_updates=context_updates,
                )

        logger.info(f"[create] Node '{mn.name}' created successfully (ip={ip})")

        return ActionResult(
            success=True,
            message=f"Node {mn.name} created on {pve_host}",
            duration=time.time() - start,
            context_updates=context_updates,
        )

    def _run_pve_lifecycle(self, exec_node: ExecutionNode, ip: str, context: dict) -> ActionResult:
        """Run PVE lifecycle phases: bootstrap, secrets, SSH keys, pve-setup,
        bridge, node config, API token, self SSH key, image download."""
        from actions.pve_lifecycle import (
            BootstrapAction,
            CopySecretsAction,
            CopySiteConfigAction,
            InjectSSHKeyAction,
            CopySSHPrivateKeyAction,
            ConfigureNetworkBridgeAction,
            GenerateNodeConfigAction,
            CreateApiTokenAction,
            InjectSelfSSHKeyAction,
        )
        from actions.recursive import RecursiveScenarioAction
        from actions.file import DownloadGitHubReleaseAction
        from actions.pve_lifecycle import _image_to_asset_name

        mn = exec_node.manifest_node
        host_key = f'{mn.name}_ip'
        start = time.time()

        # Ensure IP is in context for actions that look it up by key
        context[host_key] = ip

        # Phase sequence: list of (name, action) tuples
        phases: list[tuple[str, ActionRunner]] = []

        # 1. Bootstrap
        phases.append(('bootstrap', BootstrapAction(
            name=f'bootstrap-{mn.name}',
            host_attr=host_key,
            timeout=600,
        )))

        # 2. Copy secrets (scoped — excludes api_tokens)
        phases.append(('copy_secrets', CopySecretsAction(
            name=f'secrets-{mn.name}',
            host_attr=host_key,
        )))

        # 3. Copy site config (DNS, gateway, timezone, etc.)
        phases.append(('copy_site_config', CopySiteConfigAction(
            name=f'siteconfig-{mn.name}',
            host_attr=host_key,
        )))

        # 4. Inject driver SSH key
        phases.append(('inject_ssh_key', InjectSSHKeyAction(
            name=f'sshkey-{mn.name}',
            host_attr=host_key,
        )))

        # 5. Copy SSH private key
        phases.append(('copy_private_key', CopySSHPrivateKeyAction(
            name=f'privkey-{mn.name}',
            host_attr=host_key,
        )))

        # 6. Run pve-setup post-scenario (ansible handles privilege escalation)
        phases.append(('post_scenario', RecursiveScenarioAction(
            name=f'post-{mn.name}',
            raw_command='~/bootstrap/homestak scenario pve-setup --json-output --local --skip-preflight',
            host_attr=host_key,
            timeout=1200,
            ssh_user=self.config.automation_user,
        )))

        # 7. Configure vmbr0 bridge
        phases.append(('configure_bridge', ConfigureNetworkBridgeAction(
            name=f'network-{mn.name}',
            host_attr=host_key,
        )))

        # 8. Generate node config
        phases.append(('generate_node_config', GenerateNodeConfigAction(
            name=f'nodeconfig-{mn.name}',
            host_attr=host_key,
        )))

        # 9. Create API token
        phases.append(('create_api_token', CreateApiTokenAction(
            name=f'apitoken-{mn.name}',
            host_attr=host_key,
        )))

        # 10. Inject self SSH key
        phases.append(('inject_self_ssh_key', InjectSelfSSHKeyAction(
            name=f'selfsshkey-{mn.name}',
            host_attr=host_key,
        )))

        # 11. Download packer images for children
        for child in exec_node.children:
            child_image = child.manifest_node.image or 'debian-12'
            child_asset = _image_to_asset_name(child_image)
            phases.append((f'download_image_{child.name}', DownloadGitHubReleaseAction(
                name=f'download-image-{child.name}',
                asset_name=child_asset,
                dest_dir='/var/lib/vz/template/iso',
                host_key=host_key,
                rename_ext='.img',
                timeout=300,
            )))

        # Execute phases sequentially
        for phase_name, action in phases:
            logger.info(f"[pve-lifecycle] {mn.name}: {phase_name}")
            result = action.run(self.config, context)
            if not result.success:
                return ActionResult(
                    success=False,
                    message=f"PVE lifecycle phase '{phase_name}' failed: {result.message}",
                    duration=time.time() - start,
                )
            if result.context_updates:
                context.update(result.context_updates)

        return ActionResult(
            success=True,
            message=f"PVE lifecycle completed for {mn.name}",
            duration=time.time() - start,
        )

    def _wait_for_config_complete(
        self, exec_node: ExecutionNode, ip: str, context: dict, timeout: int = 300
    ) -> ActionResult:
        """Poll for spec fetch + config completion on a pull-mode node.

        Waits for two marker files:
        1. spec.yaml — indicates spec was fetched from server
        2. complete.json — indicates config phase completed

        Args:
            exec_node: The ExecutionNode being created
            ip: IP address of the node
            context: Shared execution context
            timeout: Max seconds to wait for each marker

        Returns:
            ActionResult indicating success/failure
        """
        from actions.ssh import WaitForFileAction

        mn = exec_node.manifest_node
        start = time.time()

        # Ensure IP is in context
        context[f'{mn.name}_ip'] = ip

        # 1. Wait for spec.yaml (fetched by cloud-init)
        wait_spec = WaitForFileAction(
            name=f'wait-spec-{mn.name}',
            host_key=f'{mn.name}_ip',
            file_path='~/.state/config/spec.yaml',
            timeout=timeout,
            interval=10,
        )
        spec_result = wait_spec.run(self.config, context)
        if not spec_result.success:
            return ActionResult(
                success=False,
                message=f"Spec not fetched on {mn.name}: {spec_result.message}",
                duration=time.time() - start,
            )

        # 2. Wait for config-complete marker (written by ./run.sh config)
        wait_config = WaitForFileAction(
            name=f'wait-config-{mn.name}',
            host_key=f'{mn.name}_ip',
            file_path='~/.state/config/complete.json',
            timeout=timeout,
            interval=10,
        )
        config_result = wait_config.run(self.config, context)
        if not config_result.success:
            return ActionResult(
                success=False,
                message=f"Config not complete on {mn.name}: {config_result.message}",
                duration=time.time() - start,
            )

        logger.info(f"[pull] Node '{mn.name}' self-configured successfully")
        return ActionResult(
            success=True,
            message=f"Pull mode config complete for {mn.name}",
            duration=time.time() - start,
        )

    def _push_config(
        self, exec_node: ExecutionNode, ip: str, context: dict, timeout: int = 300
    ) -> ActionResult:
        """Push config to a VM by running ansible from the controller.

        True push semantics: the operator resolves the spec locally, maps it
        to ansible variables, and runs ansible-playbook from the controller
        targeting the VM over SSH. No iac-driver/ansible needed on the VM.

        Steps:
        1. Resolve spec via SpecResolver
        2. Map spec to ansible vars via spec_to_ansible_vars()
        3. Run ansible-playbook from controller targeting VM
        4. Write config-complete marker on VM via SSH
        5. Verify marker

        Args:
            exec_node: The ExecutionNode being configured
            ip: IP address of the VM
            context: Shared execution context
            timeout: Max seconds to wait for config completion

        Returns:
            ActionResult indicating success/failure
        """
        import json
        import tempfile
        from actions.ssh import WaitForFileAction

        mn = exec_node.manifest_node
        start = time.time()

        logger.info(f"[push] Pushing config to {mn.name} ({ip})...")

        # 1. Resolve spec locally
        try:
            from resolver.spec_resolver import SpecResolver
            resolver = SpecResolver()
            resolved_spec = resolver.resolve(mn.spec)
        except Exception as e:
            return ActionResult(
                success=False,
                message=f"Failed to resolve spec '{mn.spec}': {e}",
                duration=time.time() - start,
            )

        # Set identity.hostname to match the node name
        if 'identity' not in resolved_spec:
            resolved_spec['identity'] = {}
        resolved_spec['identity']['hostname'] = mn.name

        # 2. Map spec to ansible vars
        from config_apply import spec_to_ansible_vars
        ansible_vars = spec_to_ansible_vars(resolved_spec)

        # 3. Write vars to temp file and run ansible-playbook from controller
        ansible_dir = get_sibling_dir('ansible')
        if not ansible_dir.exists():
            return ActionResult(
                success=False,
                message=f"Ansible directory not found: {ansible_dir}",
                duration=time.time() - start,
            )

        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False, prefix='config-vars-'
        ) as f:
            json.dump(ansible_vars, f, indent=2)
            vars_file = f.name

        user = self.config.automation_user

        # Ensure apt cache is fresh — packer cleanup removes apt lists.
        # Retry handles brief lock contention from cloud-init's apt module.
        logger.debug(f"[push] Refreshing apt cache on {mn.name}...")
        rc, _, err = run_ssh(
            ip,
            'for i in 1 2 3; do sudo apt-get update -qq 2>/dev/null && break || sleep 5; done',
            user=user, timeout=120,
        )
        if rc != 0:
            logger.warning(f"[push] apt-get update failed on {mn.name}: {err}")

        try:
            cmd = [
                'ansible-playbook',
                '-i', 'inventory/remote-dev.yml',
                'playbooks/config-apply.yml',
                '-e', f'ansible_host={ip}',
                '-e', f'ansible_user={user}',
                '--become',
                '-e', f'@{vars_file}',
            ]

            # Disable host key checking for dynamically provisioned VMs
            import os
            ansible_env = {**os.environ, 'ANSIBLE_HOST_KEY_CHECKING': 'False'}

            logger.debug(f"[push] Running ansible-playbook for {mn.name}...")
            rc, out, err = run_command(
                cmd, cwd=ansible_dir, timeout=timeout, env=ansible_env
            )
            if rc != 0:
                error_detail = err.strip() or out.strip() or 'unknown error'
                if len(error_detail) > 300:
                    error_detail = error_detail[:300] + '...'
                return ActionResult(
                    success=False,
                    message=f"Config apply failed on {mn.name}: {error_detail}",
                    duration=time.time() - start,
                )
        finally:
            import os
            os.unlink(vars_file)

        logger.debug(f"[push] Ansible completed for {mn.name}")

        # 4. Write config-complete marker on VM via SSH
        marker_json = json.dumps({
            'phase': 'config',
            'status': 'complete',
            'spec': mn.spec,
            'mode': 'push',
        })
        marker_cmd = (
            'mkdir -p ~/.state/config'
            f" && echo '{marker_json}'"
            ' > ~/.state/config/complete.json'
        )
        rc, _, err = run_ssh(ip, marker_cmd, user=user, timeout=30)
        if rc != 0:
            logger.warning(f"[push] Failed to write marker on {mn.name}: {err}")

        # 5. Verify config-complete marker
        context[f'{mn.name}_ip'] = ip
        wait_config = WaitForFileAction(
            name=f'wait-config-{mn.name}',
            host_key=f'{mn.name}_ip',
            file_path='~/.state/config/complete.json',
            timeout=60,
            interval=5,
        )
        config_result = wait_config.run(self.config, context)
        if not config_result.success:
            return ActionResult(
                success=False,
                message=f"Config marker not found on {mn.name}: {config_result.message}",
                duration=time.time() - start,
            )

        logger.info(f"[push] Node '{mn.name}' configured successfully")
        return ActionResult(
            success=True,
            message=f"Push config complete for {mn.name}",
            duration=time.time() - start,
        )

    def _handle_subtree_delegation(
        self, exec_node: ExecutionNode, context: dict,
        state: ExecutionState,
    ) -> bool:
        """Handle subtree delegation and state updates.

        Returns True on success, False on failure.
        """
        delegate_result = self._delegate_subtree(exec_node, context)
        if not delegate_result.success:
            for desc in self._get_descendants(exec_node):
                desc_state = state.get_node(desc.name)
                desc_state.fail(
                    f"Delegation failed: {delegate_result.message}")
            logger.error("Subtree delegation failed for '%s': %s",
                         exec_node.name, delegate_result.message)
            return False

        # Update state and context from delegation result
        context.update(delegate_result.context_updates or {})
        for desc in self._get_descendants(exec_node):
            desc_state = state.get_node(desc.name)
            updates = delegate_result.context_updates or {}
            desc_state.complete(
                vm_id=updates.get(f'{desc.name}_vm_id'),
                ip=updates.get(f'{desc.name}_ip'),
            )
        state.save()
        return True

    def _handle_subtree_destroy(
        self, exec_node: ExecutionNode, context: dict,
        state: ExecutionState,
    ) -> bool:
        """Handle subtree destroy delegation and state updates.

        Returns True on success, False on failure.
        """
        ip = context.get(f'{exec_node.name}_ip')
        if not ip:
            logger.warning("No IP for PVE node '%s', skipping subtree delegation",
                           exec_node.name)
            return True  # Not a failure — just nothing to delegate

        result = self._delegate_subtree_destroy(exec_node, context)
        if not result.success:
            logger.error("Subtree destroy delegation failed for '%s': %s",
                         exec_node.name, result.message)
            return False

        for desc in self._get_descendants(exec_node):
            ds = state.get_node(desc.name) if desc.name in state.nodes else state.add_node(desc.name)
            ds.mark_destroyed()
        return True

    def _delegate_subtree(self, exec_node: ExecutionNode, context: dict) -> ActionResult:
        """Delegate creation of a PVE node's children to the PVE host.

        Returns:
            ActionResult with context_updates containing descendant IPs and VM IDs
        """
        from actions.recursive import RecursiveScenarioAction

        mn = exec_node.manifest_node
        ip = context.get(f'{mn.name}_ip')
        if not ip:
            return ActionResult(
                success=False,
                message=f"No IP for PVE node '{mn.name}' in context",
                duration=0,
            )

        # Extract subtree manifest
        subtree = self.graph.extract_subtree(mn.name)
        subtree_json = subtree.to_json()

        # Build context keys to extract from result
        descendants = self._get_descendants(exec_node)
        context_keys = []
        for desc in descendants:
            context_keys.append(f'{desc.name}_ip')
            context_keys.append(f'{desc.name}_vm_id')

        # Get the hostname of the PVE node (used as -H argument)
        # The host's node config is named after its hostname
        inner_hostname = mn.name

        # Build raw command for delegation
        # Pass --self-addr so the inner executor knows its routable address
        # for HOMESTAK_SOURCE (avoids localhost propagation, #200)
        raw_cmd = (
            f'cd ~/iac/iac-driver && '
            f'./run.sh manifest apply '
            f'--manifest-json {shlex.quote(subtree_json)} '
            f'-H {shlex.quote(inner_hostname)} '
            f'--self-addr {shlex.quote(ip)} '
            f'--skip-preflight '
            f'--json-output'
        )

        logger.info(f"[delegate] Delegating subtree of '{mn.name}' ({len(descendants)} nodes)")

        action = RecursiveScenarioAction(
            name=f'delegate-{mn.name}',
            host_attr=f'{mn.name}_ip',
            raw_command=raw_cmd,
            context_keys=context_keys,
            timeout=1200,
            ssh_user=self.config.automation_user,
        )

        return action.run(self.config, context)

    def _delegate_subtree_destroy(self, exec_node: ExecutionNode, context: dict) -> ActionResult:
        """Delegate destruction of a PVE node's children to the PVE host."""
        from actions.recursive import RecursiveScenarioAction

        mn = exec_node.manifest_node
        ip = context.get(f'{mn.name}_ip')
        if not ip:
            return ActionResult(
                success=False,
                message=f"No IP for PVE node '{mn.name}' in context",
                duration=0,
            )

        # Extract subtree manifest
        subtree = self.graph.extract_subtree(mn.name)
        subtree_json = subtree.to_json()

        inner_hostname = mn.name

        raw_cmd = (
            f'cd ~/iac/iac-driver && '
            f'./run.sh manifest destroy '
            f'--manifest-json {shlex.quote(subtree_json)} '
            f'-H {shlex.quote(inner_hostname)} '
            f'--self-addr {shlex.quote(ip)} '
            f'--skip-preflight '
            f'--json-output --yes'
        )

        logger.info(f"[delegate] Delegating subtree destroy for '{mn.name}'")

        action = RecursiveScenarioAction(
            name=f'delegate-destroy-{mn.name}',
            host_attr=f'{mn.name}_ip',
            raw_command=raw_cmd,
            context_keys=[],
            timeout=600,
            ssh_user=self.config.automation_user,
        )

        return action.run(self.config, context)

    def _get_descendants(self, exec_node: ExecutionNode) -> list[ExecutionNode]:
        """Get all descendants of a node via BFS."""
        from collections import deque
        descendants: list[ExecutionNode] = []
        queue: deque[ExecutionNode] = deque(exec_node.children)
        while queue:
            node = queue.popleft()
            descendants.append(node)
            queue.extend(node.children)
        return descendants

    def _destroy_node(self, exec_node: ExecutionNode, context: dict) -> ActionResult:
        """Destroy a single node via tofu destroy."""
        from actions.tofu import TofuDestroyAction

        mn = exec_node.manifest_node
        logger.info(f"[destroy] Destroying node '{mn.name}'")

        destroy_action = TofuDestroyAction(
            name=f'destroy-{mn.name}',
            vm_name=mn.name,
            vmid=mn.vmid,
            vm_preset=mn.preset,
            image=mn.image,
            spec=mn.spec,
            manifest_name=self.manifest.name,
        )
        return destroy_action.run(self.config, context)

    def _verify_nodes(self, context: dict, state: ExecutionState) -> bool:
        """Verify SSH connectivity to all completed nodes."""
        from actions.ssh import WaitForSSHAction

        if not self.manifest.settings.verify_ssh:
            return True

        all_ok = True
        for name, node_state in state.nodes.items():
            if node_state.status != 'completed':
                continue
            ip = node_state.ip or context.get(f'{name}_ip')
            if not ip:
                logger.warning(f"No IP for node '{name}', skipping verify")
                continue

            # Ensure IP is in context under the key WaitForSSHAction expects
            context[f'{name}_ip'] = ip
            ssh_action = WaitForSSHAction(
                name=f'verify-ssh-{name}',
                host_key=f'{name}_ip',
                timeout=30,
            )
            result = ssh_action.run(self.config, context)
            if not result.success:
                logger.error(f"SSH verify failed for {name} ({ip})")
                all_ok = False

        return all_ok

    def _rollback(
        self,
        created_nodes: list[ExecutionNode],
        context: dict,
        state: ExecutionState,
    ) -> None:
        """Roll back created nodes in reverse order."""
        logger.info(f"Rolling back {len(created_nodes)} created nodes...")
        for exec_node in reversed(created_nodes):
            # If PVE node with children, delegate subtree destruction first
            if exec_node.manifest_node.type == 'pve' and exec_node.children:
                ip = context.get(f'{exec_node.name}_ip')
                if ip:
                    self._delegate_subtree_destroy(exec_node, context)

            result = self._destroy_node(exec_node, context)
            node_state = state.get_node(exec_node.name)
            if result.success:
                node_state.mark_destroyed()
            else:
                logger.error(f"Rollback destroy failed for {exec_node.name}: {result.message}")

    def _load_or_create_state(self) -> ExecutionState:
        """Try to load existing state; create fresh if not found."""
        try:
            return ExecutionState.load(self.manifest.name, self.config.name)
        except FileNotFoundError:
            state = ExecutionState(self.manifest.name, self.config.name)
            for exec_node in self.graph.create_order():
                state.add_node(exec_node.name)
            return state

    def _preview_create(self) -> None:
        """Preview create operations."""
        print("")
        print("=" * 65)
        print(f"  DRY-RUN CREATE: {self.manifest.name}")
        print(f"  Host: {self.config.name}")
        print(f"  Pattern: {self.manifest.pattern or 'flat'}")
        print("=" * 65)
        print("")
        for exec_node in self.graph.create_order():
            mn = exec_node.manifest_node
            parent_info = f" (parent: {mn.parent})" if mn.parent else " (root)"
            mode = "local" if exec_node.depth == 0 else "delegated"
            print(f"  [{exec_node.depth}] {mn.name}: {mn.type}{parent_info} [{mode}]")
            print(f"      preset={mn.preset} image={mn.image} vmid={mn.vmid}")
            if mn.type == 'pve' and exec_node.children:
                children_names = ', '.join(c.name for c in exec_node.children)
                print(f"      delegates: {children_names}")
        print("")

    def _preview_destroy(self) -> None:
        """Preview destroy operations."""
        print("")
        print("=" * 65)
        print(f"  DRY-RUN DESTROY: {self.manifest.name}")
        print(f"  Host: {self.config.name}")
        print("=" * 65)
        print("")
        for exec_node in self.graph.destroy_order():
            mn = exec_node.manifest_node
            mode = "local" if exec_node.depth == 0 else "delegated"
            print(f"  [{exec_node.depth}] {mn.name}: destroy [{mode}]")
        print("")
