import time

from novaclient import exceptions as nova_exc
from cinderclient import exceptions as cinder_exc
from rally.task import scenario
from rally.task import validation
from rally_openstack.task.scenarios.nova import utils as nova_utils


@validation.add("required_platform", platform="openstack", users=True)
@scenario.configure(
    name="CustomNova.boot_from_volume_live_migrate_and_extend",
    platform="openstack"
)
class BootFromVolumeLiveMigrateAndExtend(nova_utils.NovaScenario):

    def _get_image_by_name(self, image):
        pattern = image.get("name", "").strip("^$")
        images = list(self.clients("glance").images.list())
        for img in images:
            if img.name == pattern:
                return img
        for img in images:
            if pattern in img.name:
                return img
        raise RuntimeError("Image not found: %s" % image)

    def _get_flavor_by_name(self, flavor):
        name = flavor.get("name")
        for flv in self.clients("nova").flavors.list():
            if flv.name == name:
                return flv
        raise RuntimeError("Flavor not found: %s" % flavor)

    def _wait_for_volume_status(self, cinder, volume_id, status,
                                timeout=600, check_interval=2):
        start = time.time()
        while time.time() - start < timeout:
            try:
                vol = cinder.volumes.get(volume_id)
            except cinder_exc.NotFound:
                raise RuntimeError(
                    "Volume %s not found while waiting for status %s" %
                    (volume_id, status)
                )

            cur = (vol.status or "").lower()
            if cur == status.lower():
                return vol
            if cur == "error":
                raise RuntimeError("Volume %s entered ERROR state" % volume_id)

            time.sleep(check_interval)

        raise RuntimeError(
            "Timeout waiting for volume %s to become %s" %
            (volume_id, status)
        )

    def _wait_for_server_status(self, server_id, status,
                                timeout=600, check_interval=2):
        nova = self.clients("nova")
        start = time.time()
        while time.time() - start < timeout:
            try:
                srv = nova.servers.get(server_id)
            except nova_exc.NotFound:
                raise RuntimeError(
                    "Server %s disappeared while waiting for status %s" %
                    (server_id, status)
                )

            cur = (srv.status or "").upper()
            if cur == status.upper():
                return srv
            if cur == "ERROR":
                raise RuntimeError("Server %s entered ERROR state" % server_id)

            time.sleep(check_interval)

        raise RuntimeError(
            "Timeout waiting for server %s to become %s" %
            (server_id, status)
        )

    def _wait_for_server_delete(self, server_id,
                                timeout=600, check_interval=2):
        nova = self.clients("nova")
        start = time.time()
        while time.time() - start < timeout:
            try:
                nova.servers.get(server_id)
            except nova_exc.NotFound:
                return
            time.sleep(check_interval)
        raise RuntimeError("Timeout waiting for server %s to be deleted" %
                           server_id)

    def _wait_for_volume_delete(self, cinder, volume_id,
                                timeout=600, check_interval=2):
        start = time.time()
        while time.time() - start < timeout:
            try:
                cinder.volumes.get(volume_id)
            except cinder_exc.NotFound:
                return
            time.sleep(check_interval)
        raise RuntimeError("Timeout waiting for volume %s to be deleted" %
                           volume_id)

    def _safe_delete_server(self, server_id):
        nova = self.clients("nova")
        try:
            nova.servers.delete(server_id)
        except nova_exc.NotFound:
            return
        except Exception:
            return

        try:
            self._wait_for_server_delete(server_id, timeout=300)
        except Exception:
            pass

    def _safe_delete_volume(self, cinder, volume_id):
        try:
            start = time.time()
            while time.time() - start < 300:
                try:
                    vol = cinder.volumes.get(volume_id)
                except cinder_exc.NotFound:
                    return

                status = (vol.status or "").lower()
                if status in ("available", "error"):
                    break

                time.sleep(2)

            try:
                cinder.volumes.delete(volume_id)
            except Exception:
                try:
                    cinder.volumes.force_delete(volume_id)
                except Exception:
                    return

            try:
                self._wait_for_volume_delete(cinder, volume_id, timeout=300)
            except Exception:
                pass

        except cinder_exc.NotFound:
            return
        except Exception:
            return

    def run(self, image, flavor, volume_size, new_volume_size,
            block_migration=False, disk_over_commit=False,
            auto_assign_nic=True, volume_type=None):

        glance_image = self._get_image_by_name(image)
        nova_flavor = self._get_flavor_by_name(flavor)
        cinder = self.clients("cinder")
        nova = self.clients("nova")

        server_id = None
        volume_id = None

        try:
            # 1. Создаём bootable volume из образа
            create_args = {
                "size": volume_size,
                "imageRef": glance_image.id,
                "name": self.generate_random_name()
            }
            if volume_type:
                create_args["volume_type"] = volume_type

            volume = cinder.volumes.create(**create_args)
            volume_id = volume.id
            self._wait_for_volume_status(cinder, volume_id, "available")

            # 2. Boot from volume
            bdm = [{
                "uuid": volume_id,
                "boot_index": 0,
                "source_type": "volume",
                "destination_type": "volume",
                "delete_on_termination": False
            }]

            nics = None if auto_assign_nic else None

            server = nova.servers.create(
                name=self.generate_random_name(),
                image=None,
                flavor=nova_flavor,
                nics=nics,
                block_device_mapping_v2=bdm
            )
            server_id = server.id

            self._wait_for_server_status(server_id, "ACTIVE")
            self._wait_for_volume_status(cinder, volume_id, "in-use")

            # 3. Live migration
            nova.servers.live_migrate(
                server=server_id,
                host=None,
                block_migration=block_migration,
                disk_over_commit=disk_over_commit
            )

            self._wait_for_server_status(server_id, "ACTIVE")
            self._wait_for_volume_status(cinder, volume_id, "in-use")

            # 4. Reset-state root‑тома в available (админский трюк)
            try:
                cinder.volumes.reset_state(volume_id, state="available")
            except Exception as exc:
                raise RuntimeError(
                    "Failed to reset volume %s state to available: %s" %
                    (volume_id, exc)
                )

            self._wait_for_volume_status(cinder, volume_id, "available")

            # 5. Extend до new_volume_size
            if new_volume_size and new_volume_size > volume_size:
                cinder.volumes.extend(volume_id, new_volume_size)

                # ждём, пока том перейдёт в приемлемое состояние
                start = time.time()
                while time.time() - start < 600:
                    vol = cinder.volumes.get(volume_id)
                    status = (vol.status or "").lower()
                    if status in ("available", "in-use"):
                        break
                    if status == "error":
                        raise RuntimeError(
                            "Volume %s entered ERROR after extend" % volume_id
                        )
                    time.sleep(2)

                final_vol = cinder.volumes.get(volume_id)
                if int(final_vol.size) != int(new_volume_size):
                    raise RuntimeError(
                        "Volume %s size is %s, expected %s"
                        % (volume_id, final_vol.size, new_volume_size)
                    )

        finally:
            if server_id:
                self._safe_delete_server(server_id)
            if volume_id:
                self._safe_delete_volume(cinder, volume_id)
