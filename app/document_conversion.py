import io
import os
import shutil
import signal
import subprocess
import uuid
import zipfile

DEFAULT_CONVERSION_ROOT = "/data/data/com.termux/files/home/data/conversions"


def convert_documents_batch(file_list, *, conversion_root=DEFAULT_CONVERSION_ROOT):
    """
    file_list: list of tuples (original_filename, file_bytes)
    Returns: (output_bytes, output_filename, mime_type, error_str)
    """
    if not file_list:
        return None, None, None, "No files provided"

    conv_root = conversion_root
    os.makedirs(conv_root, exist_ok=True)
    batch_id = str(uuid.uuid4())[:8]
    batch_dir = os.path.join(conv_root, f"batch_{batch_id}")
    os.makedirs(batch_dir, exist_ok=True)

    input_paths = []
    file_meta = []  # (safe_prefix, orig_base_name, in_path)

    for i, (fn, fb) in enumerate(file_list):
        safe_orig = os.path.basename(fn).strip() or f"document_{i+1}"
        base_name, ext = os.path.splitext(safe_orig)
        if not ext:
            ext = ".docx"
        safe_prefix = f"doc_{i:03d}"
        in_filename = f"{safe_prefix}{ext}"
        in_path = os.path.join(batch_dir, in_filename)
        with open(in_path, "wb") as f:
            f.write(fb)
        input_paths.append(in_path)
        file_meta.append((safe_prefix, base_name, in_path))

    try:
        timeout_sec = min(35 + len(file_list) * 12, 300)
        cmd = [
            "proot-distro", "login", "debian", "--",
            "libreoffice", "-env:UserInstallation=file://" + batch_dir + "/profile", "--headless", "--convert-to", "pdf"
        ] + input_paths + ["--outdir", batch_dir]

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        res = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)

        converted_items = []
        for safe_prefix, orig_base, in_path in file_meta:
            expected_pdf = os.path.join(batch_dir, f"{safe_prefix}.pdf")
            if os.path.exists(expected_pdf):
                with open(expected_pdf, "rb") as pf:
                    pdf_bytes = pf.read()
                out_name = f"{orig_base}.pdf"
                converted_items.append((out_name, pdf_bytes))

        if not converted_items:
            err_msg = res.stderr or res.stdout or "Conversion engine produced no output"
            return None, None, None, f"Batch conversion error: {err_msg[:250]}"

        if len(file_list) == 1 and len(converted_items) == 1:
            out_name, pdf_bytes = converted_items[0]
            return pdf_bytes, out_name, "application/pdf", None

        zip_buffer = io.BytesIO()
        used_names = {}
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for out_name, pdf_bytes in converted_items:
                final_name = out_name
                if final_name in used_names:
                    used_names[final_name] += 1
                    b, e = os.path.splitext(final_name)
                    final_name = f"{b}_{used_names[final_name]}{e}"
                else:
                    used_names[final_name] = 1
                zf.writestr(final_name, pdf_bytes)

        zip_data = zip_buffer.getvalue()
        return zip_data, "converted_documents.zip", "application/zip", None

    except subprocess.TimeoutExpired:
        return None, None, None, f"Conversion timed out for {len(file_list)} files"
    except Exception as e:
        return None, None, None, str(e)
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)
