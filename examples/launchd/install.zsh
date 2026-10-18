#!/bin/zsh
# 登録: zsh install.zsh <ワークスペース>（config.yaml のあるディレクトリ）
# 本体を ~/.seat-analyzer/scheduled/ に写し、plist の値を埋めて LaunchAgents に登録する。
# 再実行で入れ替える。解除: zsh install.zsh --uninstall（本体とログは残す）。

set -eu

if (( $# != 1 )) || [[ "$1" == -* && "$1" != --uninstall ]]; then
    print -u2 -- '使い方: zsh install.zsh <ワークスペース> | --uninstall'
    exit 2
fi
if [[ "$OSTYPE" != darwin* ]]; then
    print -u2 -- 'macOS で実行してください。'
    exit 1
fi

template_dir=${0:A:h}
agents="$HOME/Library/LaunchAgents"
scheduled="$HOME/.seat-analyzer/scheduled"
logs="$HOME/.seat-analyzer/logs"
domain="gui/$(id -u)"
labels=(seat-analyzer.collect-weekly seat-analyzer.collect-monthly)

if [[ "$1" == --uninstall ]]; then
    for label in "${labels[@]}"; do
        launchctl bootout "$domain/$label" 2>/dev/null || true
        rm -f -- "$agents/$label.plist"
    done
    print -- "解除しました。本体（${scheduled}/）とログ（${logs}/）は残しています。"
    exit 0
fi

workspace=${1:A}
if [[ ! -d "$workspace" || ! -f "$workspace/config.yaml" ]]; then
    print -u2 -- 'config.yaml のあるワークスペースのディレクトリを指定してください。'
    exit 1
fi

# パスに含まれる XML の特殊文字を、テキスト要素に埋められる形にする。
xml_escape() {
    REPLY=${1//&/\&amp;}
    REPLY=${REPLY//</\&lt;}
    REPLY=${REPLY//>/\&gt;}
}
xml_escape "$HOME"
xml_home=$REPLY
xml_escape "$workspace"
xml_workspace=$REPLY

mkdir -p -- "$agents" "$scheduled" "$logs"
temporary=''
trap '[[ -z "$temporary" ]] || rm -f -- "$temporary"' EXIT
temporary=$(mktemp "$scheduled/collect.zsh.XXXXXX")
cp -- "$template_dir/collect.zsh" "$temporary"
mv -f -- "$temporary" "$scheduled/collect.zsh"
temporary=''

for label in "${labels[@]}"; do
    content=$(<"$template_dir/$label.plist")
    content=${content//__HOME__/$xml_home}
    content=${content//__WORKSPACE__/$xml_workspace}
    temporary=$(mktemp "$agents/$label.plist.XXXXXX")
    print -r -- "$content" > "$temporary"
    if ! plutil -lint -s "$temporary"; then
        print -u2 -- 'plist の検査に失敗しました。'
        exit 1
    fi
    mv -f -- "$temporary" "$agents/$label.plist"
    temporary=''
    launchctl bootout "$domain/$label" 2>/dev/null || true
    if ! launchctl bootstrap "$domain" "$agents/$label.plist"; then
        print -u2 -- "${label} の登録に失敗しました。"
        exit 1
    fi
done

for label in "${labels[@]}"; do
    print -- "登録: ${label}"
    print -- "確認: launchctl print ${domain}/${label}"
    print -- "1 回実行: launchctl kickstart ${domain}/${label}"
done
print -- "ログ: ${logs}/collect-weekly.log・collect-monthly.log"
