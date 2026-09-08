#!/usr/bin/env bash
# Open your Reddit saved page in the default browser.
cd "$(dirname "$(readlink -f "$0")")/.."

REDDIT_USERNAME="YOUR_REDDIT_USERNAME"
if [ -f .env ]; then
  val="$(sed -n 's/^REDDIT_USERNAME=//p' .env | tail -n1)"
  val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
  [ -n "$val" ] && REDDIT_USERNAME="$val"
fi

xdg-open "https://old.reddit.com/user/${REDDIT_USERNAME}/saved" >/dev/null 2>&1 &
