import cv2
import pandas as pd
import shutil
import os

# ==============================
# CSV FILE
# ==============================
CSV_FILE = "op.csv"

# ==============================
# CHECK CSV
# ==============================
if not os.path.exists(CSV_FILE):
    print(f"'{CSV_FILE}' not found!")
    exit()

# ==============================
# READ CSV
# ==============================
df = pd.read_csv(CSV_FILE, header=None, names=["Machine", "Object"])

df["Machine"] = df["Machine"].astype(str).str.strip()
df["Object"] = df["Object"].astype(str).str.strip()

# ==============================
# DISPLAY MACHINES
# ==============================
print("\nAvailable Machines")
print("-" * 40)

for i, row in df.iterrows():
    print(f"{i+1}. {row['Machine']}   -->   {row['Object']}")

print("-" * 40)

# ==============================
# SELECT MACHINE
# ==============================
choice = input("Select Machine (Number or Name): ").strip()

if choice.isdigit():

    index = int(choice) - 1

    if index < 0 or index >= len(df):
        print("Invalid machine number!")
        exit()

    row_index = index
    machine = df.loc[row_index, "Machine"]

else:

    machine = choice.upper()

    if machine not in df["Machine"].values:
        print("Machine not found!")
        exit()

    row_index = df[df["Machine"] == machine].index[0]

old_op = df.loc[row_index, "Object"]

print("\nSelected Machine :", machine)
print("Current Object   :", old_op)

# ==============================
# CAMERA
# ==============================
print("\nOpening Camera...")
print("Show a NEW OP QR Code")
print("Press Q to Quit\n")

detector = cv2.QRCodeDetector()

cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("Cannot open camera.")
    exit()

new_op = None

while True:

    ret, frame = cap.read()

    if not ret:
        break

    data, points, _ = detector.detectAndDecode(frame)

    if data:

        if points is not None:

            points = points.astype(int)

            for i in range(4):
                pt1 = tuple(points[0][i])
                pt2 = tuple(points[0][(i + 1) % 4])

                cv2.line(frame, pt1, pt2, (0, 255, 0), 2)

        cv2.putText(frame,
                    data,
                    (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1,
                    (0, 255, 0),
                    2)

        # Accept only OP QR Codes
        if data.upper().startswith("OP"):

            new_op = data.upper()

            # Check duplicate
            duplicate = df[df["Object"] == new_op]

            if not duplicate.empty:

                assigned_machine = duplicate.iloc[0]["Machine"]

                if assigned_machine != machine:

                    print("\n===================================")
                    print(f"{new_op} already belongs to {assigned_machine}")
                    print("Update Cancelled")
                    print("===================================")

                    cap.release()
                    cv2.destroyAllWindows()
                    exit()

            break

    cv2.imshow("QR Scanner", frame)

    key = cv2.waitKey(1)

    if key == ord('q') or key == ord('Q'):
        break

cap.release()
cv2.destroyAllWindows()

# ==============================
# QR NOT FOUND
# ==============================
if new_op is None:
    print("No QR detected.")
    exit()

print("\nDetected QR :", new_op)

# ==============================
# CONFIRM
# ==============================
confirm = input(f"\nReplace {old_op} with {new_op}? (Y/N): ").strip().lower()

if confirm != "y":
    print("Cancelled.")
    exit()

# ==============================
# BACKUP
# ==============================
shutil.copy(CSV_FILE, "op_backup.csv")

# ==============================
# UPDATE CSV
# ==============================
df.loc[row_index, "Object"] = new_op

df.to_csv(CSV_FILE, header=False, index=False)

# ==============================
# SUCCESS
# ==============================
print("\n===================================")
print("Updated Successfully")
print("===================================")
print("Machine :", machine)
print("Old OP  :", old_op)
print("New OP  :", new_op)
print("CSV     :", CSV_FILE)
print("Backup  : op_backup.csv")
print("===================================")