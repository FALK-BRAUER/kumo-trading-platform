# The IB gateway is COMPOSE'S, and there is only one mechanism

`scripts/ib_gateway.py` was deleted on 2026-08-24. It used Nautilus's `DockerizedIBGateway` to create a
container named `nautilus-ib-gateway-<mode>`, outside compose.

## Why it had to go rather than be tidied

Two mechanisms created a gateway for one job, and **compose could not see the one that won**:

```
nautilus-ib-gateway-paper   created by the script   bridge network, host port 127.0.0.1:4002
                            LOGGED IN, holding the session
kumo-paper-ib-gateway-1     declared in compose     broker network, no host port
                            stuck five days on "Existing session detected"
```

Only one session may hold the paper login. The ad-hoc container took it, and the declared one sat unhealthy
from 2026-08-18 to 2026-08-23 with nobody able to say why — `docker compose ps` does not list a
container it did not create.

## And it is structurally wrong for more than one instance

The script's container name and host port are **fixed**. `test-alpaca` and `staging-ibkr` cannot each
have one; they would collide on the name, the port, and the IB login. A compose service is per-project
by construction — `kumo-staging-ib-gateway-1` is a different container from `kumo-test-ib-gateway-1`,
on a different isolated network, with no host port at all.

## What to use instead

```
make up INSTANCE=staging-ibkr        # instance.env sets KUMO_EXEC=ibkr
```

which adds `--profile ibkr`, so compose starts `ib-gateway` on the project's own `broker` network. The
engine reaches it at `ib-gateway:4004`; nothing else can reach it, because it publishes no host port.

Credentials come from the keychain at deploy time and the preflight refuses without them:

```
export TWS_USERID=<your-paper-username>
export TWS_PASSWORD=$(security find-generic-password -s ibkr-ibc-paper -w)
export IBKR_ACCOUNT_ID=$(security find-generic-password -s ibkr-account-paper -w)
```

kumo-cockpit#486.
