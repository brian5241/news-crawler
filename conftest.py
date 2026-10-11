"""pytest 가 프로젝트 루트의 crawler 패키지를 찾도록 경로를 넣는다."""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
