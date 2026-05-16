"""简易五子棋 - 双人对战，命令行版"""
from typing import Optional

BOARD_SIZE = 15


class Gomoku:
    def __init__(self):
        self.board = [["." for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
        self.current = "X"

    def display(self):
        print("   " + " ".join(f"{i:2}" for i in range(BOARD_SIZE)))
        for r, row in enumerate(self.board):
            print(f"{r:2} " + "  ".join(row))

    def place(self, row: int, col: int) -> bool:
        if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
            print("坐标超出范围")
            return False
        if self.board[row][col] != ".":
            print("此位置已有棋子")
            return False
        self.board[row][col] = self.current
        return True

    def check_win(self, row: int, col: int) -> bool:
        directions = [(0, 1), (1, 0), (1, 1), (1, -1)]
        for dr, dc in directions:
            count = 1
            for delta in (1, -1):
                r, c = row + dr * delta, col + dc * delta
                while 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE and self.board[r][c] == self.current:
                    count += 1
                    r += dr * delta
                    c += dc * delta
            if count >= 5:
                return True
        return False

    def is_full(self) -> bool:
        return all(cell != "." for row in self.board for cell in row)

    def run(self):
        print("五子棋 - 15x15\n输入格式: 行 列 (如 7 7)，输入 q 退出")
        while True:
            self.display()
            print(f"\n当前玩家: {self.current}")

            inp = input("> ").strip()
            if inp.lower() == "q":
                print("游戏结束")
                break

            parts = inp.split()
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                print("格式错误，请用: 行 列")
                continue

            row, col = int(parts[0]), int(parts[1])
            if not self.place(row, col):
                continue

            if self.check_win(row, col):
                self.display()
                print(f"\n玩家 {self.current} 赢了!")
                break

            if self.is_full():
                self.display()
                print("\n平局!")
                break

            self.current = "O" if self.current == "X" else "X"


if __name__ == "__main__":
    Gomoku().run()
