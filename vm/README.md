# Playing in a Windows 98 VM

Windows 98 can't use VirtualBox shared folders, so the game travels on a small
virtual hard disk (`ic2disk.vhd`, 64 MB FAT16) that the VM sees as drive D:.

## Setup (once per VM)

```
py vm/ic2disk.py build
```

With the VM **powered off**, attach the disk as a second IDE hard disk
(VirtualBox Manager > Settings > Storage > IDE > Add hard disk, or):

```
VBoxManage storageattach windows98 --storagectl IDE --port 0 --device 1 --type hdd --medium vm/ic2disk.vhd
```

Boot Windows 98. The game is at `D:\IC2\Imperial Conquest 2.exe`, and the
existing saves are in `D:\IC2\saves`. Save new games anywhere on D:.

## Getting saves back

1. In Windows 98: Start > Shut Down (this flushes the disk cache).
2. On the host, with the VM off:

   ```
   py vm/ic2disk.py pull
   ```

   This copies every `*.sav` on D: into `saves/`. Saves already moved to
   `saves-processed/` are skipped. `py vm/ic2disk.py list` shows the whole disk.

To use it without Python, open `ic2disk.vhd` in 7-Zip, or double-click it in
Windows Explorer to mount it.

## Rebuilding

`build` won't overwrite an existing disk. Run `pull` first, then
`build --force`. VirtualBox remembers each disk's UUID, so remove the old one
under File > Tools > Media before you attach the new one.
