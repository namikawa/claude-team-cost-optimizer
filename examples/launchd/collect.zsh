#!/bin/zsh
# launchd から weekly（当月）または monthly（前月）を引数にして呼ぶ。
# CSV の取得に続けてログインを確認し、どちらかの終了コードが非 0 のときだけ通知する。
# SEAT_ANALYZER_CMD はコマンド（既定 seat-analyzer、空白で単語分割、引用符は解釈しない）。
# SEAT_ANALYZER_INPUT_DIR は取得先の上書き。作業ディレクトリは plist のワークスペースを使う。
# 取得できる時期はツールが判定する。install.zsh が ~/.seat-analyzer/scheduled/ に写す。

set -u

if (( $# != 1 )) || [[ "$1" != weekly && "$1" != monthly ]]; then
    print -u2 -- '使い方: zsh collect.zsh <weekly|monthly>'
    exit 2
fi
mode=$1
cmd=(${=SEAT_ANALYZER_CMD:-seat-analyzer})
collect_args=(collect --source claude)
collect_name='当月の取得'
failures=()

log() {
    print -- "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

log "=== ${mode} 開始 ==="
if [[ "$mode" == monthly ]]; then
    ym=$(date +%Y-%m)
    year=${ym%-*}
    month=${ym#*-}
    (( year = 10#$year, month = 10#$month - 1 ))
    if (( month == 0 )); then
        (( year -= 1, month = 12 ))
    fi
    printf -v previous '%04d-%02d' "$year" "$month"
    collect_args+=(--month "$previous")
    collect_name="前月（${previous}）の取得"
fi
if [[ -n "${SEAT_ANALYZER_INPUT_DIR:-}" ]]; then
    collect_args+=(--input-dir "$SEAT_ANALYZER_INPUT_DIR")
fi

if ! "${cmd[@]}" "${collect_args[@]}"; then
    failures+=("$collect_name")
fi
if ! "${cmd[@]}" collect --source claude --check-login; then
    failures+=('ログインの確認')
fi

if (( ${#failures} )); then
    failed_names=${(j:・:)failures}
    log "=== ${mode} 終了: 要対応（${failed_names}）==="
    if ! osascript -e 'on run argv' \
        -e 'display notification (item 1 of argv) with title "seat-analyzer 定期取得" sound name "Basso"' \
        -e 'end run' "要対応: ${failed_names}。ログは ~/.seat-analyzer/logs/ にあります"; then
        log 'macOS の通知に失敗しました'
    fi
    exit 1
fi
log "=== ${mode} 終了: 正常 ==="
exit 0
