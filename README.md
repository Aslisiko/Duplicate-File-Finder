# Duplicate File Finder v2.0 🚀

A powerful and fast tool to find and manage duplicate files, optimized for large media libraries (Videos & RAW photos). This app features a smart three-pass hashing system to handle GB-sized files in seconds and includes full Hebrew (RTL) support.

---

## Features ✨
* **Smart Hashing:** Uses a three-pass system (Size -> Sample -> Full MD5) to avoid unnecessary reading of large files.
* **Hebrew Support:** Full BiDi and RTL support for filenames and interface.
* **Safety First:** Files are moved to the system Trash instead of permanent deletion.
* **Smart Selection:** Automatically select older or newer copies for quick cleanup.
* **Image/Video Preview:** Open files directly from the app to verify content.

---

## Installation & Setup 🛠️

Follow these steps to set up the project on your local machine. It is highly recommended to use a Virtual Environment (VENV).

### 1. Clone the repository
```bash
git clone [https://github.com/Aslisiko/Duplicate-File-Finder.git](https://github.com/Aslisiko/Duplicate-File-Finder.git)
cd Duplicate-File-Finder


### 2. Create and Activate Virtual Environment (VENV)
Windows (PowerShell):

PowerShell
# Create the environment
python -m venv venv

# Activate the environment
.\venv\Scripts\activate
Linux / Mac:

Bash
# Create the environment
python3 -m venv venv

# Activate the environment
source venv/bin/activate


### 3. Install Required Dependencies
Once the environment is active, install all necessary libraries:

Bash
pip install customtkinter send2trash python-bidi arabic-reshaper Pillow


### 4. Run the Application
Bash
python main.py


### Requirements 📦
The application relies on the following Python libraries:

customtkinter - Modern and dark-mode UI.

send2trash - Safe deletion to system trash.

python-bidi & arabic-reshaper - Proper Hebrew text rendering.

Pillow - Image handling for previews.

License 📜
Distributed under the MIT License. See LICENSE for more information.

Developed by Asaf - Feel free to contribute or report issues!
