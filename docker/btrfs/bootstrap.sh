#!/bin/sh
set -eu
mount --make-rprivate /
for name in dev proc sys; do
    mount --rbind "/$name" "/task-root/$name"
    mount --make-rprivate "/task-root/$name"
done
for name in hosts hostname resolv.conf; do
    touch "/task-root/etc/$name"
    mount --bind "/etc/$name" "/task-root/etc/$name"
done
cd /task-root
pivot_root . .
/bin/umount -l .
cd /
exec /bin/sleep infinity
