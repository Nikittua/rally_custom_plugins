mkdir -p ~/.rally/plugins

vi ~/.rally/plugins/custom_nova_volume.py
rally plugin list --name CustomNova

rally plugin show CustomNova.boot_from_volume_live_migrate_and_extend

cat boot_from_volume_live_migrate_and_extend.yaml
---
CustomNova.boot_from_volume_live_migrate_and_extend:
  - args:
      image:
        name: "^Ubuntu-22.04-LTS-cloud$"
      flavor:
        name: "m1.2"
      volume_size: 20
      new_volume_size: 30
      auto_assign_nic: true
      block_migration: false
      disk_over_commit: false
    runner:
      type: "constant"
      times: 1
      concurrency: 1
    sla:
      failure_rate:
        max: 0



rally task start boot_from_volume_live_migrate_and_extend.yaml
