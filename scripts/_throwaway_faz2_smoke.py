"""Throwaway — FAZ2 PR-review enable spot-check. Bu PR MERGE EDİLMEYECEK, kapatılacak."""


def collect(item, bucket=[]):
    # spot-check yemi: mutable default arg (paylaşılan liste footgun'u)
    bucket.append(item)
    return bucket


def ratio(a, b):
    # spot-check yemi: b==0 zero-division guard yok
    return a / b
