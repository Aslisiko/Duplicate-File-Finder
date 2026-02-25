# Project: Duplicate File Finder (Cross-Platform)

## Goal

Create a Python GUI application that identifies duplicate files based on size and content (MD5 hash).

## Technical Requirements

- Language: Python 3
- GUI Library: CustomTkinter
- Path Management: Use `pathlib` for Windows/Linux compatibility.
- Environment: Developed on Linux, Target: Windows.

## Features

1. Directory Selector: Button to choose which folder to scan.
2. Scan Logic:
   - First, group files by size.
   - For files with identical sizes, perform an MD5 hash check to confirm they are duplicates.
3. Results UI: A scrollable list showing:
   - File Name
   - Path
   - Size (in MB/KB)
4. Actions:
   - Ability to select specific duplicates and delete them.
   - A "Select All Duplicates" button.

## Instructions for Claude

Please provide the complete Python code in a single file named `main.py`. Ensure the code is well-commented and handles permission errors (e.g., when trying to read system files) gracefully.
