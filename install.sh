#!/usr/bin/env bash

. scripts/cmd/clashctl.sh
. scripts/preflight.sh

_valid
_parse_args "$@"

_prepare_zip
_detect_init

_okcat "安装内核：$KERNEL_NAME by ${INIT_TYPE}"
_okcat '📦' "安装路径：$CLASH_BASE_DIR"

/bin/cp -rf . "$CLASH_BASE_DIR"
[ -f "$CLASH_CONFIG_MIXIN" ] || /bin/cp "${CLASH_CONFIG_MIXIN%.yaml}.example.yaml" "$CLASH_CONFIG_MIXIN"
[ -f "$CLASH_PROFILES_META" ] || /bin/cp "${CLASH_PROFILES_META%.yaml}.example.yaml" "$CLASH_PROFILES_META"
touch "$CLASH_CONFIG_BASE"
_set_envs
_is_regular_sudo && chown -R "$SUDO_USER" "$CLASH_BASE_DIR"
_grant_tun_capability

_install_service
_apply_rc


_merge_config
_detect_proxy_port
clashui
clashsecret "$(_get_random_val)" >/dev/null
clashsecret

_okcat '🎉' 'enjoy 🎉'
clashctl

_valid_config "$CLASH_CONFIG_BASE" && CLASH_CONFIG_URL="file://$CLASH_CONFIG_BASE"
_quit "clashsub add $CLASH_CONFIG_URL && clashsub use 1"
