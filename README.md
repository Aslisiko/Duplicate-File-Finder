# Duplicate File Finder v2.0 🚀

A powerful and fast tool to find and manage duplicate files, optimized for large media libraries (Videos & RAW photos). This app features a smart three-pass hashing system to handle GB-sized files in seconds and includes full Hebrew (RTL) support.

## Features
* **Smart Hashing:** Uses a three-pass system (Size -> Sample -> Full MD5) to avoid unnecessary reading of large files.
* **Hebrew Support:** Full BiDi and RTL support for filenames and interface.
* **Safety First:** Files are moved to the system Trash instead of permanent deletion.
* **Smart Selection:** Automatically select older or newer copies for quick cleanup.
* **Image/Video Preview:** Open files directly from the app to verify content.

---

## Installation & Setup 🛠️

Follow these steps to set up the project on your local machine:

### 1. Clone the repository
```bash
git clone [https://github.com/Aslisiko/Duplicate-File-Finder.git](https://github.com/Aslisiko/Duplicate-File-Finder.git)
cd Duplicate-File-Finder
2. Create and activate a Virtual Environment (VENV)
Windows:

PowerShell
python -m venv venv
.\venv\Scripts\activate
Linux / Mac:

Bash
python3 -m venv venv
source venv/bin/activate
3. Install dependencies
Bash
pip install -r requirements.txt
4. Run the application
Bash
python main.py
Requirements
The following libraries are required (automatically installed via requirements.txt):

customtkinter (Modern UI)

send2trash (Safe deletion)

python-bidi & arabic-reshaper (Hebrew support)

Pillow (Image handling)

License
Distributed under the MIT License. See LICENSE for more information.
