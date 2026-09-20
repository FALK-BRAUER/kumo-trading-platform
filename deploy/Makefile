# kumo-trading-platform deploy (#21) — isolated paper + prod stacks. Run from deploy/.
# Paper and prod are separate compose projects (kumo-paper / kumo-prod): own networks, volumes, ports.
# There is no shared toggle — each target maps to one hardwired compose file.

PAPER := docker compose --env-file .env.paper -f compose.paper.yml
PROD  := docker compose --env-file .env.prod  -f compose.prod.yml

BACKEND_REV = $$(git log -1 --format=%h -- ../backend ../deploy/Dockerfile.backend ../deploy/compose.paper.yml 2>/dev/null || echo unknown)
UI_REV      = $$(git log -1 --format=%h -- ../ui ../deploy/Dockerfile.ui 2>/dev/null || echo unknown)

# `BUILD_ID` is the UI's stamp and Next's deterministic buildId. It belongs in STAMP so EVERY path that
# builds the ui carries it (#374). `deploy-ui-paper` set it and `up-paper` did not, so a full stack bring-up
# produced a ui image with an EMPTY revision label — which `verify-stamp-paper` then reported as a mismatch
# against a UI that was current, the exact cry-wolf the check was fixed to stop raising.
UI_BUILD_ID = BUILD_ID=$(UI_REV)$$(git diff --quiet 2>/dev/null || echo -dirty)

# Commit stamps baked into the backend image (#329) so a running container can NAME the code it is running.
# Recursive (=) not immediate (:=) so the SHA is read at recipe time, not at Makefile parse time — a parse
# after `git checkout` would otherwise stamp the commit you LEFT. `--dirty` because an uncommitted local
# edit that reaches the image must not be reported as a clean commit; a stamp you cannot trust is worse
# than "unknown". `2>/dev/null || echo unknown` keeps a build working outside a git checkout.
# STAMPED WITH THE SAME REV THE CHECK WANTS (#374). This used to stamp HEAD while `verify-stamp-paper`
# compared against the last BACKEND-touching commit, so any commit that changed only the UI or this
# Makefile desynchronised them and reported a mismatch on an image that was current. Two derivations of
# one fact, and they disagreed the first time they were asked to.
STAMP = KUMO_GIT_SHA=$(BACKEND_REV)$$(git diff --quiet 2>/dev/null || echo -dirty) \
        KUMO_STRATEGIES_SHA=$$(git -C ../../kumo-trading-strategies rev-parse --short HEAD 2>/dev/null || echo unknown)$$(git -C ../../kumo-trading-strategies diff --quiet 2>/dev/null || echo -dirty)

# Gateway-free by default (exec=Alpaca, #23). TWS_*/IBKR_ACCOUNT_ID only needed for `--profile ibkr`.
REQUIRED := APCA_API_KEY_ID APCA_API_SECRET_KEY

# TELEGRAM CREDENTIALS, READ AT RECIPE TIME FROM THE KEYCHAIN (2026-08-21).
#
# compose.paper.yml passes these through as `${VAR:-}`. That default is an EMPTY STRING, not an error,
# so a bring-up from a shell that never exported them produced four variables of length 0 inside the
# container and a notifier that dropped every alert. Meanwhile the notifications settings domain read
# `enabled: true`, so the UI said alerts were on. Nothing in the stack disagreed with anything else.
#
# These are NOT added to REQUIRED: alerts must never block a deploy. They are exported if present and
# warned about if absent (`preflight-telegram`), which is the loud-but-not-fatal middle the old setup
# was missing entirely.
#
# `-a ledger-tool` is the account, matching ledger-tool/tools/scripts/notify-telegram.sh so no second bot and
# no second keychain entry are needed. KUMO_* is exported the same way for a stack that overrides
# `telegram_account`; absent entries simply stay empty.
TELEGRAM = LEDGER_TOOL_TELEGRAM_BOT_TOKEN=$$(security find-generic-password -a ledger_tool -s LEDGER_TOOL_TELEGRAM_BOT_TOKEN -w 2>/dev/null) \
           LEDGER_TOOL_TELEGRAM_CHAT_ID=$$(security find-generic-password -a ledger_tool -s LEDGER_TOOL_TELEGRAM_CHAT_ID -w 2>/dev/null) \
           KUMO_TELEGRAM_BOT_TOKEN=$$(security find-generic-password -a kumo -s KUMO_TELEGRAM_BOT_TOKEN -w 2>/dev/null) \
           KUMO_TELEGRAM_CHAT_ID=$$(security find-generic-password -a kumo -s KUMO_TELEGRAM_CHAT_ID -w 2>/dev/null)

# Never prints a value — only whether one was found. A preflight that echoes the secret it is checking
# is worse than no preflight.
define preflight-telegram
	@t=$$(security find-generic-password -a ledger_tool -s LEDGER_TOOL_TELEGRAM_BOT_TOKEN -w 2>/dev/null); \
	 c=$$(security find-generic-password -a ledger_tool -s LEDGER_TOOL_TELEGRAM_CHAT_ID -w 2>/dev/null); \
	 if [ -n "$$t" ] && [ -n "$$c" ]; then echo "telegram: credentials found (ledger-tool)"; \
	 else echo "telegram: WARNING — LEDGER_TOOL_TELEGRAM_{BOT_TOKEN,CHAT_ID} not in the keychain;"; \
	      echo "          alerts will be DROPPED even though notifications.enabled may be true."; fi
endef

# check-env <file>: fail if the file is missing or any required var is blank
# WHAT IS ACTUALLY BEING BUILT (#484). Refuse rather than build the wrong thing.
#
# 2026-08-22: `git checkout main` in the primary repo FAILED because a worktree held the branch. The
# error scrolled past, and an hour later a build from that stale tree produced a log BYTE-IDENTICAL to a
# correct one. Only the image stamp knew — after the fact, on a running stack. A guard before the build
# turns "caught afterwards by one artifact check" into "cannot happen".
#
# THE STRATEGIES TREE IS THE BUILD CONTEXT, not a pin. `Dockerfile.backend` does
# `COPY --from=strategies . /tmp/kumo-trading-strategies`, so the image ships whatever is in that working
# directory — uncommitted edits included. `KUMO_STRATEGIES_SHA` records `-dirty` but not WHAT was dirty,
# so a stamp cannot recover it. That tree was clean on the 22nd by luck, not process.
#
# ALLOW_DIRTY=1 is a LOUD OVERRIDE, not a bypass: "test this before I push" is a legitimate workflow, and
# a rule that forbids it gets deleted by whoever needs it at 3am. It prints what it permitted, so the
# choice lands in the build log instead of in someone's memory.
define check-tree
	@set -e; \
	ok=1; dirty=0; pin_fail=0; \
	cockpit_head=$$(git rev-parse HEAD); \
	origin_main=$$(git rev-parse origin/main 2>/dev/null || echo unknown); \
	if [ "$$cockpit_head" != "$$origin_main" ]; then \
	  echo "REFUSING: cockpit tree is at $${cockpit_head} but origin/main is $${origin_main}."; \
	  echo "  A build from here ships code that is not on main. If a worktree holds the branch,"; \
	  echo "  \`git worktree list\` names it. Use \`git checkout --detach origin/main\` to build main."; \
	  ok=0; \
	fi; \
	if ! git diff --quiet HEAD 2>/dev/null || [ -n "$$(git status --porcelain 2>/dev/null)" ]; then \
	  echo "REFUSING: cockpit tree is DIRTY — uncommitted changes would ship with no commit to trace."; \
	  dirty=1; \
	fi; \
	sdir=../../kumo-trading-strategies; \
	if [ -d "$$sdir/.git" ]; then \
	  if [ -n "$$(git -C $$sdir status --porcelain 2>/dev/null)" ]; then \
	    echo "REFUSING: kumo-trading-strategies tree is DIRTY. It IS the build context (COPY --from=strategies),"; \
	    echo "  so uncommitted work ships into the image and the stamp records only '-dirty'."; \
	    echo "  THIS ONE HAS NO OVERRIDE. Dirty is not more dangerous than unpushed, it is"; \
	    echo "  UNTRACEABLE BY CONSTRUCTION: no commit exists to point at, so permitting it"; \
	    echo "  only records that we chose to ship something unknown. Commit it."; \
	    dirty=1; \
	  fi; \
	  git -C $$sdir fetch origin --quiet 2>/dev/null || true; \
	  behind=$$(git -C $$sdir rev-list --count HEAD..origin/main 2>/dev/null || echo 0); \
	  if [ "$$behind" != "0" ]; then \
	    echo "REFUSING: kumo-trading-strategies is $$behind commit(s) BEHIND origin/main."; \
	    echo "  The build takes the LOCAL tree, so pushing is not enough — pull it."; \
	    ok=0; \
	  fi; \
	  pinned=$$(sed -n "s/.*kumo-trading-strategies\.git@\([0-9a-f]\{7,40\}\)['\"].*/\1/p" ../backend/pyproject.toml | head -1); \
	  if [ -z "$$pinned" ]; then \
	    echo "REFUSING: the kumo-trading-strategies PIN could not be parsed from backend/pyproject.toml (#850)."; \
	    echo "  The gate that compares the checkout with the pin has nothing to compare against —"; \
	    echo "  a branch name, a reformatted line or a missing line all land here. Fix the pin line."; \
	    pin_fail=1; \
	  elif ! git -C $$sdir merge-base --is-ancestor $$pinned HEAD 2>/dev/null; then \
	    echo "REFUSING: kumo-trading-strategies checkout does not contain the PINNED $$pinned (backend/pyproject.toml)."; \
	    echo "  The pin is decorative for the image — the LOCAL tree is what ships — so a checkout"; \
	    echo "  behind the pin deploys code the tests never saw (#845 review; 2087b85 reached paper this way)."; \
	    pin_fail=1; \
	  elif [ "$$(git -C $$sdir rev-parse HEAD | cut -c1-$${#pinned})" != "$$pinned" ]; then \
	    if [ -n "$(ALLOW_AHEAD_OF_PIN)" ]; then \
	      echo "ALLOW_AHEAD_OF_PIN=$(ALLOW_AHEAD_OF_PIN) — kumo-trading-strategies checkout $$(git -C $$sdir rev-parse --short HEAD) is AHEAD of the pin $$pinned; shipping it deliberately."; \
	    else \
	      echo "REFUSING: kumo-trading-strategies checkout $$(git -C $$sdir rev-parse --short HEAD) is AHEAD of the PINNED $$pinned (#850)."; \
	      echo "  A tree past the pin ships code the tests never saw — the same sentence as 'behind'."; \
	      echo "  Move the pin (backend/pyproject.toml + uv.lock) or check out the pin; ALLOW_AHEAD_OF_PIN=1 is the loud override."; \
	      pin_fail=1; \
	    fi; \
	  fi; \
	else \
	  echo "REFUSING: no kumo-trading-strategies checkout at $$sdir — it is the build context and must exist."; \
	  ok=0; \
	fi; \
	if [ "$$dirty" = "1" ]; then \
	  echo ""; \
	  echo "Nothing was built. A DIRTY tree has no override, by design."; \
	  exit 1; \
	fi; \
	if [ "$$pin_fail" = "1" ]; then \
	  echo ""; \
	  echo "Nothing was built. The strategies PIN gate has no override (#850): ALLOW_UNPUSHED is for an"; \
	  echo "unpushed COCKPIT commit and must not waive a strategies tree the tests never saw."; \
	  exit 1; \
	fi; \
	if [ "$$ok" = "0" ]; then \
	  if [ -n "$(ALLOW_UNPUSHED)" ]; then \
	    echo ""; \
	    echo "ALLOW_UNPUSHED=$(ALLOW_UNPUSHED) — building from a commit not on origin/main."; \
	    echo "  Traceable in principle: the SHA exists. Recorded so the choice is in the build log."; \
	  else \
	    echo ""; \
	    echo "Nothing was built. Override with ALLOW_UNPUSHED=1 for a deliberate hotfix."; \
	    exit 1; \
	  fi; \
	fi
endef

define check-env
	@test -f $(1) || { echo "missing deploy/$(1) (copy $(1).example)"; exit 1; }
	@set -a; . ./$(1); set +a; for v in $(REQUIRED); do \
	  eval "val=\$$$$v"; [ -n "$$val" ] || { echo "$(1): $$v is empty"; exit 1; }; done
endef

.PHONY: up-paper deploy-ui-paper down-paper logs-paper build-paper up-prod down-prod logs-prod build-prod

## --- PAPER ---------------------------------------------------------------
build-paper:
	$(call check-tree)
	$(STAMP) $(UI_BUILD_ID) $(PAPER) build
up-paper:
	$(call check-tree)
	$(call check-env,.env.paper)
	$(call preflight-telegram)
	$(STAMP) $(UI_BUILD_ID) $(TELEGRAM) $(PAPER) up -d --build --remove-orphans
	@echo "paper up → UI http://localhost:3000  API http://localhost:8000"
	@$(MAKE) --no-print-directory verify-stamp-paper
	@$(MAKE) --no-print-directory verify-stack-paper
# What commit is ACTUALLY running (#329). Reads the label off the image the container is USING, then
# compares it to the working tree. `up -d --build` above rebuilds, but a build that silently no-ops and a
# build that worked look identical in the log — so verify by asking the artifact, not by trusting the step.
# DOES THE RUNNING STACK MATCH THE COMPOSE FILE? (#419)
#
# `verify-stamp-paper` below answers "was this image built from current code". It cannot answer this
# one, for two structural reasons: it iterates a HARDCODED list (`engine api ui`), so a service outside
# that list is invisible whatever it does; and an image label says nothing about the compose config a
# container was CREATED with.
#
# Both blind spots fired on 2026-08-21. The `rotation` sidecar was deleted from compose and merged to
# main, and its container went on running and serving the Market tab off a host mount — `main` said the
# sidecar was gone, the machine said it was serving, and nothing reported the disagreement. The same
# day the engine gained a `ledger-tool/tools` mount and the api lost `KUMO_ROTATION_PATH`; neither
# container knew. Operator: "We have that problem again and again."
#
# Editing compose changes nothing about what is already running — docker does not reconcile a live
# stack against a file. So the file has to be compared to the stack, and it has to happen on every
# deploy rather than when someone remembers.
#
# `--remove-orphans` is on `up-paper` above for the same reason: an orphan is not just undetected, it
# is un-removed. Compose warns about them and carries on.
#
# THE SAME ENV AS THE BUILD, OR THE CHECK LIES. `up-paper` runs compose with $(TELEGRAM) exported;
# this recomputed the service config WITHOUT it, so the two hashes could never match for the only two
# services that reference those variables — measured 2026-08-23:
#
#     without TELEGRAM   api 49082db  engine 63cfa41   postgres 0ea6647
#     with    TELEGRAM   api 70dfc5b  engine 563cabf   postgres 0ea6647   <- postgres identical
#
# Postgres is byte-identical either way because it declares no telegram vars, which is exactly why only
# api and engine were ever reported STALE. It was not drift; it was this recipe asking a different
# question from the one `up-paper` answered.
#
# A staleness check that cries wolf after every clean deploy is one nobody reads on the morning it is
# right, and both reviewers independently said "the checker is wrong" was the conclusion they trusted
# least because it requires no further work. It required this much.
.PHONY: verify-stack-paper
verify-stack-paper:
	@$(TELEGRAM) ../backend/.venv/bin/python ../backend/scripts/verify_stack.py \
	   --file compose.paper.yml --env-file .env.paper --project kumo-paper 2>/dev/null \
	 || { echo "  ^^ the running stack does not match compose.paper.yml"; exit 1; }

.PHONY: verify-stamp-paper
# COMPARE AGAINST THE CODE THE IMAGE CONTAINS, NOT AGAINST HEAD (#354).
#
# This compared every image to `HEAD`, so a UI-only commit reported the backend as a MISMATCH while the
# backend it was running was byte-identical. An alarm that fires on the healthy case is one an operator
# learns to scroll past — and this is the single check that answers "what code is actually deployed",
# so training anyone to ignore it costs more than the check is worth.
#
# The right comparand for each image is the last commit that touched ITS OWN sources. A genuinely stale
# image still fails: if backend changes at X and the image predates it, `want` is X and the stamp is not.
# TWO SOURCE TREES GO INTO THIS IMAGE AND ONLY ONE WAS EVER CHECKED (#358 follow-up).
#
# `Dockerfile.backend` installs kumo-trading-strategies from a LOCAL CHECKOUT passed as a build context, so the
# `pyproject.toml` pin is decorative — whatever is in ../../kumo-trading-strategies at build time is what runs.
# This target compared only the cockpit revision, so a stale, wrong or uncommitted strategies tree passed
# clean. That is how kumo-trading-strategies 2087b85 reached the paper engine unnoticed.
#
# The dirty check is separate and deliberate: a `-dirty` suffix means the running strategy code exists in
# no commit anywhere, so an incident cannot be traced to a diff. It reports without failing the target —
# building from a dirty checkout is normal mid-session and blocking it would train an operator to skip
# the check that also catches the stale case.
STRATEGIES_REV = $$(git -C ../../kumo-trading-strategies rev-parse --short HEAD 2>/dev/null || echo unknown)$$(git -C ../../kumo-trading-strategies diff --quiet 2>/dev/null || echo -dirty)
verify-stamp-paper:
	@dirty="$$(git diff --quiet 2>/dev/null || echo -dirty)"; \
	 for svc in engine api ui; do \
	   case $$svc in ui) want="$(UI_REV)$$dirty";; *) want="$(BACKEND_REV)$$dirty";; esac; \
	   img=$$(docker inspect --format '{{.Image}}' kumo-paper-$$svc-1 2>/dev/null) || continue; \
	   got=$$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' $$img 2>/dev/null); \
	   if [ "$$got" = "$$want" ]; then echo "  $$svc: $$got  OK"; \
	   else echo "  $$svc: running $$got, working tree is $$want  ** MISMATCH **"; fail=1; fi; \
	   case $$svc in ui) ;; *) \
	     sgot=$$(docker image inspect --format '{{index .Config.Labels "com.kumo.strategies.revision"}}' $$img 2>/dev/null); \
	     if [ "$$sgot" = "$(STRATEGIES_REV)" ]; then echo "    strategies: $$sgot  OK"; \
	     else echo "    strategies: running $$sgot, checkout is $(STRATEGIES_REV)  ** MISMATCH **"; fail=1; fi; \
	     case "$$sgot" in unknown|"") echo "    strategies: the image cannot name its strategies revision"; fail=1;; esac; \
	     case "$$sgot" in *-dirty) echo "    strategies: built from UNCOMMITTED code — $$sgot names nothing reproducible"; dirtyfail=1;; esac; \
	   ;; esac; \
	 done; \
	 [ -z "$$dirtyfail" ] || echo "the strategies half of this image cannot be reproduced from any commit"; \
	 [ -z "$$fail" ] || { echo "deployed image does not match the working tree — do not report this as deployed"; exit 1; }
# UI-ONLY deploy: rebuild + recreate JUST the ui container, leaving the engine (and any resting orders)
# untouched. Use this for every frontend change — `up-paper` recreates the engine, which re-reconciles
# against the broker and churns the order blotter. `--no-deps` stops compose from also touching engine/api.
deploy-ui-paper:
	BUILD_ID=$(UI_REV)$$(git diff --quiet 2>/dev/null || echo -dirty) $(PAPER) build ui
	$(PAPER) up -d --no-deps ui
	@echo "paper UI redeployed (engine untouched, build $(UI_REV)) → http://localhost:3000"
	@$(MAKE) --no-print-directory verify-stamp-paper
down-paper:
	$(PAPER) down
logs-paper:
	$(PAPER) logs -f

## --- PROD (LIVE, real money) --------------------------------------------
build-prod:
	$(PROD) build
up-prod:
	$(call check-env,.env.prod)
	@printf "\033[31mThis starts the LIVE (real-money) stack.\033[0m Type LIVE to confirm: "; \
	 read ans; [ "$$ans" = "LIVE" ] || { echo "aborted."; exit 1; }
	$(PROD) up -d
	@echo "PROD up → UI http://localhost:3001  API http://localhost:8001  (LIVE)"
down-prod:
	$(PROD) down
logs-prod:
	$(PROD) logs -f
