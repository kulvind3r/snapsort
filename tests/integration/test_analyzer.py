from analyzer import get_similarity_clusters
from pathlib import Path


def test_near_duplicates_form_cluster(tmp_path, make_jpeg_file):
    make_jpeg_file("a.jpg", color=(200, 30, 30))
    make_jpeg_file("b.jpg", color=(202, 30, 30))   # near-identical
    make_jpeg_file("c.jpg", color=(10, 200, 10))   # clearly different
    clusters = get_similarity_clusters(str(tmp_path))
    assert len(clusters) >= 1
    names = {Path(m.path).name for cl in clusters for m in cl.members}
    assert "a.jpg" in names and "b.jpg" in names


def test_blurry_image_ranked_last(tmp_path, make_jpeg_file):
    make_jpeg_file("sharp.jpg", color=(200, 30, 30), blur=False)
    make_jpeg_file("blurry.jpg", color=(200, 30, 30), blur=True)
    clusters = get_similarity_clusters(str(tmp_path))
    assert clusters, "Expected a similarity cluster"
    best = clusters[0].best_pick()
    assert best is not None
    assert "sharp" in best.path