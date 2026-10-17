import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from v2l.features import features
from v2l.signals import Signals


def face_sequence(angles, shifts=0.0, scales=1.0, translations=(320, 80, 0), jaw_drop=0.0):
    """A fixed face in image-coordinate units, with independent mouth translation and head pose."""
    angles = np.asarray(angles, dtype=float)
    n = len(angles)
    base = np.zeros((478, 3))
    points = {
        10: (0, 0, -10), 152: (0, 200, 10), 2: (0, 125, 25),
        13: (0, 150, 25), 14: (0, 160, 25), 61: (-35, 155, 20), 291: (35, 155, 20),
        33: (-60, 60, 0), 133: (-20, 60, 0), 263: (60, 60, 0), 362: (20, 60, 0),
        160: (-50, 55, 0), 158: (-30, 55, 0), 144: (-50, 65, 0), 153: (-30, 65, 0),
        159: (-40, 55, 0), 145: (-40, 65, 0), 468: (-40, 60, 0),
        387: (50, 55, 0), 385: (30, 55, 0), 373: (50, 65, 0), 380: (30, 65, 0),
        386: (40, 55, 0), 374: (40, 65, 0), 473: (40, 60, 0),
    }
    for index, point in points.items():
        base[index] = point
    p = np.broadcast_to(base, (n, 478, 3)).copy()
    shift = np.broadcast_to(np.asarray(shifts), (n,))
    for index in (13, 14, 61, 291):
        p[:, index, 0] += shift * 120  # known interocular distance
    drop = np.broadcast_to(np.asarray(jaw_drop), (n,))
    p[:, 152, 1] += drop
    p[:, 14, 1] += drop / 2
    scale = np.broadcast_to(np.asarray(scales), (n,))
    rotation = Rotation.from_euler('xyz', angles, degrees=True).as_matrix()
    p = np.einsum('nij,npj->npi', rotation, p) * scale[:, None, None]
    p += np.broadcast_to(np.asarray(translations), (n, 3))[:, None, :]

    # Head matrices are in camera coordinates; landmarks are image Y-down, depth-away.
    camera_to_image = np.diag([1.0, -1.0, -1.0])
    head = np.broadcast_to(np.eye(4), (n, 4, 4)).copy()
    head[:, :3, :3] = (camera_to_image @ rotation @ camera_to_image) * scale[:, None, None]
    pose = np.zeros((n, 33, 4))
    pose[:, 11] = [0.3, 0.5, 0, 1]
    pose[:, 12] = [0.7, 0.5, 0, 1]
    return Signals(
        t=np.arange(n) / 30, size=np.array([480, 640]), bs_names=np.array(['_neutral']),
        bs=np.zeros((n, 1)), head_mat=head, face_lm=p / np.array([640, 480, 640]),
        face_ok=np.ones(n, dtype=bool), pose_img=pose, pose_world=pose.copy(),
        pose_ok=np.ones(n, dtype=bool),
    )


@pytest.mark.parametrize('axis', [0, 1, 2])
def test_rigid_head_rotation_does_not_invent_lateral_lip_motion(axis):
    angles = np.zeros((13, 3))
    angles[:, axis] = np.linspace(-30, 30, 13)
    f = features(face_sequence(angles))
    np.testing.assert_allclose(f['mouthShift'], 0, atol=1e-12)


def test_real_lateral_lip_motion_survives_rotation_translation_and_scale():
    angles = [[0, 0, 0], [15, -25, 20], [-20, 30, -15], [25, 15, 30], [-10, -30, 25]]
    shifts = np.array([-0.08, -0.03, 0, 0.03, 0.08])
    scales = [0.7, 1.5, 1.0, 0.9, 1.3]
    translations = [[300, 80, 0], [310, 90, 25], [280, 100, -30], [330, 70, 10], [320, 110, 50]]
    f = features(face_sequence(angles, shifts, scales, translations))
    np.testing.assert_allclose(f['mouthShift'], shifts, atol=1e-12)


def test_vertical_jaw_and_lip_opening_do_not_create_sideways_motion():
    angles = np.tile([15, 25, -20], (5, 1))
    f = features(face_sequence(angles, shifts=0.025, jaw_drop=[0, 5, 15, 25, 0]))
    np.testing.assert_allclose(f['mouthShift'], 0.025, atol=1e-12)
    assert f['jawDrop'][3] > f['jawDrop'][0]
    assert f['lipGap'][3] > f['lipGap'][0]


def test_mouth_shift_is_missing_without_a_valid_head_pose():
    s = face_sequence([[0, 0, 0], [0, 0, 0], [10, 20, 15]], shifts=0.04)
    s.face_ok[1] = False
    s.head_mat[1] = np.nan
    f = features(s)
    assert np.isnan(f['mouthShift'][1])
    np.testing.assert_allclose(f['mouthShift'][[0, 2]], 0.04, atol=1e-12)
