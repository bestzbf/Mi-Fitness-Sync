#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
task_desktop_app="$HOME/Applications/小米跑步同步.app"
if [[ -d "$task_desktop_app" ]]; then
  open "$task_desktop_app"
  exit $?
fi
if [[ -d "dist/小米跑步同步.app" ]]; then
  open "dist/小米跑步同步.app"
  exit $?
fi
if [[ ! -x .venv/bin/python ]]; then
  print "当前目录缺少 Python 环境，请先安装 Mac 发行版，或按 README 建立 Python 环境。"
  read -r 'task_close_reply?按回车结束：'
  exit 1
fi
.venv/bin/python -u scripts/setup_cn.py
task_exit_status=$?
read -r 'task_close_reply?按回车结束：'
exit "$task_exit_status"
