#!/usr/bin/env python3

def decide(history, current_question):
    # Adjusted threshold to improve performance
    threshold = 0.55
    scores = [p[1] for p in history]
    decisions = ['yes' if s >= threshold else 'no' for s in scores]
    return decisions

if __name__ == '__main__':
    # Minimal smoke-test harness to verify that the module runs without errors
    import sys
    try:
        result = decide([], {})  # placeholder arguments for demonstration
        print(result)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
