import hashlib
import os
import tempfile

import requests


DATAVERSE_URL = "https://dataverse.harvard.edu"
SFD_DOI = "10.7910/DVN/EWCNL5"
USER_AGENT = "Teglon/0.2 (https://github.com/phelipedarc/teglon_O4_updated) sne@lco.global"


def _download_file(url, destination, expected_md5):
    if os.path.isfile(destination):
        digest = hashlib.md5()
        with open(destination, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() == expected_md5:
            print("File exists. Not overwriting.")
            return

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=os.path.dirname(destination))
    os.close(fd)
    digest = hashlib.md5()
    try:
        with requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            stream=True,
            timeout=60,
        ) as response:
            response.raise_for_status()
            with open(temporary_name, "wb") as destination_file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        destination_file.write(chunk)
                        digest.update(chunk)
        if digest.hexdigest() != expected_md5:
            raise RuntimeError(
                "Downloaded SFD file has an unexpected MD5 checksum: {} (expected {})".format(
                    digest.hexdigest(), expected_md5
                )
            )
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def fetch_sfd(data_dir):
    metadata_url = "{}/api/datasets/:persistentId".format(DATAVERSE_URL)
    response = requests.get(
        metadata_url,
        params={"persistentId": "doi:" + SFD_DOI},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    metadata = response.json()
    files = metadata["data"]["latestVersion"]["files"]
    files_by_name = {entry["dataFile"]["filename"]: entry["dataFile"] for entry in files}

    sfd_dir = os.path.join(data_dir, "sfd")
    for pole in ("ngp", "sgp"):
        filename = "SFD_dust_4096_{}.fits".format(pole)
        file_metadata = files_by_name[filename]
        destination = os.path.join(sfd_dir, filename)
        print("Downloading SFD data file to {}".format(destination))
        _download_file(
            "{}/api/access/datafile/{}".format(DATAVERSE_URL, file_metadata["id"]),
            destination,
            file_metadata["md5"],
        )