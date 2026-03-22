# Troubleshooting

Common issues and their solutions when running iac-driver operations.

## Preflight Failures

Preflight validation runs before scenario execution. Use `--skip-preflight` to
bypass when needed, but fix the underlying issue before production runs.

**API token not found:** Secrets are not decrypted. Run `cd $HOMESTAK_ROOT/config &&
make decrypt`. If the token is rejected (401), regenerate on the PVE host with
`pveum user token add root@pam homestak --privsep 0`, update `secrets.yaml`, and
run `make encrypt`.

**Cannot connect to API endpoint:** Verify the PVE host is online, `pveproxy` is
running, firewall allows port 8006, and `api_endpoint` in
`config/nodes/<host>.yaml` matches the actual IP.

**Host not resolvable:** The hostname does not resolve via DNS. Add a DNS entry,
add to `/etc/hosts`, or use `--host <ip>` with a raw IP address.

**Missing packer images:** The executor downloads images automatically during PVE
lifecycle. For manual download, use GitHub release assets from
`homestak-iac/packer`.

## Tofu State Issues

**Lock contention** (`Error acquiring the state lock`): Another tofu process holds
the lock. Check with `ps aux | grep tofu`. If stale (process crashed), remove the
lock file from `$HOMESTAK_ROOT/.state/tofu/<manifest>-<host>/<vm-name>/`.

**Stale state:** If a VM was manually deleted but state still references it, remove
the stale resource:
```bash
cd $HOMESTAK_ROOT/.state/tofu/<manifest>-<host>/<vm-name>/
tofu state rm proxmox_virtual_environment_vm.vm
```

**Provider version mismatch:** The bpg/proxmoxve provider is pinned. After a tofu
upgrade, run `cd $HOMESTAK_ROOT/iac/tofu && tofu init -upgrade` to update the lock file.

## Server Daemon

The server daemon serves specs and config bundles over HTTPS. The executor starts
it automatically during manifest operations.

**Port already in use:** Another server instance is running. Run `./run.sh server
stop`. If the PID file is stale, remove `$HOMESTAK_ROOT/.state/server/server.pid`.

**Stale bare repos:** The server caches repo clones. Send SIGHUP to refresh without
restarting:
```bash
./run.sh server status    # Shows PID
kill -HUP <pid>
```

**Self-signed certificate warnings:** Expected in dev. Clients use `insecure=True`
by default to skip verification.

## SSH Issues

**RSA key required for bpg/proxmox provider:** The Go SSH library cannot parse
ed25519 keys. The provider needs `~/.ssh/id_rsa`. If you see "attempted methods
[none]" during tofu apply, generate an RSA key:
```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa
```
Ensure the RSA pubkey is in `secrets.yaml` under `ssh_keys`.

**PVE replaces root authorized_keys:** PVE installation overwrites
`/root/.ssh/authorized_keys`. The PVE lifecycle actions (`InjectSSHKeyAction`,
`InjectSelfSSHKeyAction`) re-add keys after setup. If SSH breaks mid-lifecycle,
use the PVE web console to re-add the key manually.

**SSH host key changed after reprovision:** After destroying and recreating a VM,
clear the old key with `ssh-keygen -R <host-or-ip>`.

## Cloud-init Issues

**HOMESTAK_SERVER not injected:** Check that `server_url` is configured in
`config/site.yaml`:
```yaml
defaults:
  server_url: "https://198.51.100.61:44443"
```
Without this, ConfigResolver does not mint provisioning tokens and tofu does not
inject `HOMESTAK_SERVER` or `HOMESTAK_TOKEN` into cloud-init.

**VM boots but config apply fails:** Check the cloud-init log on the VM:
```bash
ssh homestak@<ip> cat /var/log/cloud-init-output.log
```
Common causes: server daemon not running on the controller, token signing key
mismatch, or spec FK referencing a nonexistent `config/specs/*.yaml` file.

**Config-complete marker never appears:** The executor polls for
`$HOMESTAK_ROOT/.state/config/complete.json`. SSH into the VM to diagnose:
```bash
ls -la $HOMESTAK_ROOT/.state/config/                   # Was spec fetched?
journalctl -u vm-config.service           # Pull mode oneshot log
journalctl -u pve-config.service          # PVE mode oneshot log
```
For PVE nodes, check `$HOMESTAK_ROOT/.state/pve-config/failure.json` for the failed phase and
error message.
