def merge_intervals(intervals):
    """Merge overlapping or touching closed intervals [a, b]; return them sorted."""
    result = []
    for start, end in intervals:
        if result and start < result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
