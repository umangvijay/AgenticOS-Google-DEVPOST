import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

try:  # the GCP client library is only required when the manager is used
    from google.cloud import secretmanager
except ImportError:  # pragma: no cover - environments without GCP extras
    secretmanager = None

def _require_library():
    if secretmanager is None:
        raise RuntimeError("google-cloud-secret-manager is not installed")

class GCPSecretManager:
    """Wrapper for Google Cloud Secret Manager."""

    def __init__(self):
        self.project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
        self.client = None
        if self.project_id and secretmanager is not None:
            try:
                self.client = secretmanager.SecretManagerServiceClient()
                logger.info(f"GCPSecretManager initialized for project {self.project_id}")
            except Exception as e:
                logger.error(f"Failed to initialize SecretManager: {e}")
        else:
            logger.warning("GOOGLE_CLOUD_PROJECT not set, GCPSecretManager disabled.")

    def _get_secret_path(self, secret_id: str) -> str:
        return f"projects/{self.project_id}/secrets/{secret_id}"

    def get_secret(self, secret_id: str) -> str:
        if not self.client:
            raise RuntimeError("GCPSecretManager is not initialized.")

        name = f"{self._get_secret_path(secret_id)}/versions/latest"
        response = self.client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")

    def get_secret_version(self, secret_id: str, version: str) -> str:
        """Read an exactly-pinned secret version (Phase 30 / 30.7).

        Version-qualified references keep stored records resolving to the
        version they were written against even after upstream rotation."""
        if not self.client:
            raise RuntimeError("GCPSecretManager is not initialized.")
        if not version or version == "latest":
            return self.get_secret(secret_id)
        name = f"{self._get_secret_path(secret_id)}/versions/{version}"
        response = self.client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")

    def set_secret(self, secret_id: str, payload: str) -> Optional[str]:
        """Add a new secret version. Returns the pinned version identifier
        of the stored value so callers can persist a version-qualified
        reference; None when the version could not be determined."""
        if not self.client:
            raise RuntimeError("GCPSecretManager is not initialized.")

        parent = f"projects/{self.project_id}"

        # Check if secret exists
        try:
            self.client.get_secret(request={"name": self._get_secret_path(secret_id)})
        except Exception:
            # Create the secret if it doesn't exist
            self.client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_id,
                    "secret": {"replication": {"automatic": {}}},
                }
            )

        # Add a new version
        response = self.client.add_secret_version(
            request={
                "parent": self._get_secret_path(secret_id),
                "payload": {"data": payload.encode("UTF-8")},
            }
        )
        try:
            # resource name looks like projects/p/secrets/s/versions/N
            return response.name.rsplit("/", 1)[-1] or None
        except Exception:
            return None

    def delete_secret(self, secret_id: str) -> None:
        if not self.client:
            raise RuntimeError("GCPSecretManager is not initialized.")
            
        try:
            self.client.delete_secret(request={"name": self._get_secret_path(secret_id)})
        except Exception as e:
            logger.warning(f"Failed to delete secret {secret_id}: {e}")
