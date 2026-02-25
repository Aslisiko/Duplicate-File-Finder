# Duplicate File Finder — Help

Version: 2.0  
Platform: Linux / Windows (also works on macOS)

## What this app does

This app scans a folder and finds **duplicate files** (same content), then lets you safely send selected duplicates to your system Trash/Recycle Bin.

It uses a fast 3-pass method:
1. Group by file size
2. Compare first 10 KB sample
3. Verify full MD5 hash

---

## Requirements

- Python 3.10+
- Packages:
  - `customtkinter`
  - `send2trash`
  - `python-bidi` (optional, for better Hebrew/RTL display)
  - `arabic-reshaper` (optional, for better Hebrew/RTL display)

Install:

```bash
pip install customtkinter send2trash python-bidi arabic-reshaper
```

---

## Run the program

From the project folder:

```bash
python main.py
```

---

## How to use

1. Click **📂 Choose Directory** and select a folder.
2. Click **🔍 Scan for Duplicates**.
3. Wait for scan to complete (progress bar + status text shown).
4. Review duplicate groups in the results list.
5. Select files to remove:
   - **☑ Select All**: mark all files.
   - **🕐 Select Older**: keep newest file in each group, mark older copies.
   - **🕒 Select Newer**: keep oldest file in each group, mark newer copies.
6. (Optional) Click **👁** to preview/open a file with your system default app.
7. Click **🗑 Send to Trash** to move selected files to Trash/Recycle Bin.

After deletion, the app automatically rescans the folder.

---

## Interface notes

- **Summary line** shows:
  - Number of duplicate groups
  - Number of duplicate copies
  - Estimated wasted space
- In each group, the **oldest file name is highlighted** (amber color) when dates differ.

---

## Safety behavior

- Deletion is **not permanent** when `send2trash` is installed.
- Files are moved to system Trash/Recycle Bin and can usually be restored.
- If `send2trash` is missing, the app shows an error and does not delete.

---

## Troubleshooting

### 1) App does not start

- Verify Python and dependencies are installed:

```bash
python --version
pip show customtkinter send2trash
```

### 2) “send2trash not installed” warning

Install it:

```bash
pip install send2trash
```

Restart the app after installation.

### 3) Some files are skipped

The scanner ignores files it cannot access (permission errors, locked files, OS errors).

### 4) Preview button does nothing

- Linux: requires `xdg-open`
- Windows: uses default file association
- macOS: uses `open`

Make sure your system has a default application for that file type.

---

## Tips

- Start scanning a specific subfolder (not your whole disk) for better speed.
- Use **Select Older** for typical cleanup while keeping latest edits.
- Double-check file paths before sending to Trash.

---

## Main file

- Application entry point: `main.py`
