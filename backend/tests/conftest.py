import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="adi-test-"))
os.environ["ADI_DATABASE_URL"] = f"sqlite:///{_tmp / 'test.db'}"
os.environ["ADI_STORAGE_DIR"] = str(_tmp / "storage")
os.environ.pop("ADI_AZURE_DI_ENDPOINT", None)  # tests never call Azure; its HTTP is mocked
os.environ.pop("ADI_AZURE_DI_KEY", None)

import pytest  # noqa: E402

from tests import samples  # noqa: E402


@pytest.fixture(scope="session")
def sample_dir() -> Path:
    d = _tmp / "samples"
    d.mkdir(exist_ok=True)
    (d / "invoice_digital.pdf").write_bytes(samples.invoice_pdf())
    (d / "quotation_digital.pdf").write_bytes(samples.quotation_pdf())
    (d / "invoice_scanned.pdf").write_bytes(samples.scanned_invoice_pdf())
    (d / "letter.pdf").write_bytes(samples.letter_pdf())
    (d / "invoice_email.html").write_text(samples.INVOICE_EMAIL_HTML)
    return d
