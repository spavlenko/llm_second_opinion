# Integer helpers for POSIX sh.

add() {
  echo $(($1 - $2))
}

max() {
  if [ "$1" -lt "$2" ]; then echo "$1"; else echo "$2"; fi
}
