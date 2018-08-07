#!/usr/bin/env python3

import itertools
import logging
import os
import random
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
import urllib
import zipfile

import mutagen
import requests

import r128gain
import r128gain.opusgain


IS_TRAVIS = os.getenv("CI") and os.getenv("TRAVIS")


def download(url, filepath):
  cache_dir = os.getenv("TEST_DL_CACHE_DIR")
  if cache_dir is not None:
    os.makedirs(cache_dir, exist_ok=True)
    cache_filepath = os.path.join(cache_dir,
                                  os.path.basename(urllib.parse.urlsplit(url).path))
    if os.path.isfile(cache_filepath):
      shutil.copyfile(cache_filepath, filepath)
      return
  with requests.get(url, stream=True) as response, open(filepath, "wb") as file:
    for chunk in response.iter_content(chunk_size=2 ** 14):
      file.write(chunk)
  if cache_dir is not None:
    shutil.copyfile(filepath, cache_filepath)


class TestR128Gain(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    cls.ref_temp_dir = tempfile.TemporaryDirectory()

    vorbis_filepath = os.path.join(cls.ref_temp_dir.name, "f.ogg")
    download("https://upload.wikimedia.org/wikipedia/en/0/09/Opeth_-_Deliverance.ogg",
             vorbis_filepath)

    opus_filepath = os.path.join(cls.ref_temp_dir.name, "f.opus")
    download("https://people.xiph.org/~giles/2012/opus/ehren-paper_lights-64.opus",
             opus_filepath)

    mp3_filepath = os.path.join(cls.ref_temp_dir.name, "f.mp3")
    download("http://www.largesound.com/ashborytour/sound/brobob.mp3",
             mp3_filepath)

    m4a_filepath = os.path.join(cls.ref_temp_dir.name, "f.m4a")
    download("https://auphonic.com/media/audio-examples/01.auphonic-demo-unprocessed.m4a",
             m4a_filepath)

    flac_filepath = os.path.join(cls.ref_temp_dir.name, "f.flac")
    flac_zic_filepath = "%s.zip" % (flac_filepath)
    download("http://helpguide.sony.net/high-res/sample1/v1/data/Sample_HisokanaMizugame_88_2kHz24bit.flac.zip",
             flac_zic_filepath)
    with zipfile.ZipFile(flac_zic_filepath) as zip_file:
      in_filename = zip_file.namelist()[0]
      with zip_file.open(in_filename) as in_file:
        with open(flac_filepath, "wb") as out_file:
          shutil.copyfileobj(in_file, out_file)
    os.remove(flac_zic_filepath)

    wv_filepath = os.path.join(cls.ref_temp_dir.name, "f.wv")
    cmd = ("sox", "-R",
           "-n", "-b", "16", "-c", "2", "-r", "44.1k", "-t", "wv", wv_filepath,
           "synth", "30", "sine", "1-5000")
    subprocess.check_call(cmd)

  @classmethod
  def tearDownClass(cls):
    cls.ref_temp_dir.cleanup()

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    for src_filename in os.listdir(__class__.ref_temp_dir.name):
      shutil.copy(os.path.join(__class__.ref_temp_dir.name, src_filename), self.temp_dir.name)

    self.vorbis_filepath = os.path.join(self.temp_dir.name, "f.ogg")
    self.opus_filepath = os.path.join(self.temp_dir.name, "f.opus")
    self.mp3_filepath = os.path.join(self.temp_dir.name, "f.mp3")
    self.m4a_filepath = os.path.join(self.temp_dir.name, "f.m4a")
    self.flac_filepath = os.path.join(self.temp_dir.name, "f.flac")
    self.wv_filepath = os.path.join(self.temp_dir.name, "f.wv")

    self.ref_levels = {self.vorbis_filepath: (-7.7, 2.6),
                       self.opus_filepath: (-14.7, None),
                       self.mp3_filepath: (-13.9, -0.5),
                       self.m4a_filepath: (-20.6, 0.1),
                       self.flac_filepath: (-26.7, -12.7),
                       self.wv_filepath: (-3.3, -3.0),
                       0: (-11.4, 2.6)}

    self.max_peak_filepath = self.vorbis_filepath

  def tearDown(self):
    self.temp_dir.cleanup()

  def test_scan(self):
    for album_gain in (False, True):
      filepaths = (self.vorbis_filepath,
                   self.opus_filepath,
                   self.mp3_filepath,
                   self.m4a_filepath,
                   self.flac_filepath,
                   self.wv_filepath)
      ref_levels = self.ref_levels.copy()
      if not album_gain:
        del ref_levels[r128gain.ALBUM_GAIN_KEY]
      self.assertEqual(r128gain.scan(filepaths,
                                     album_gain=album_gain),
                       ref_levels)

      if album_gain:
        # file order should not change results
        filepaths = (self.vorbis_filepath,  # reduce permutation counts to speed up tests
                     self.opus_filepath,
                     self.flac_filepath)
        ref_levels = r128gain.scan(filepaths,
                                   album_gain=True)
        if IS_TRAVIS:
          shuffled_filepaths_len = len(tuple(itertools.permutations(filepaths)))
        for i, shuffled_filepaths in enumerate(itertools.permutations(filepaths), 1):
          if shuffled_filepaths == filepaths:
            continue
          if IS_TRAVIS:
            print("Testing permutation %u/%u..." % (i, shuffled_filepaths_len))
          self.assertEqual(r128gain.scan(shuffled_filepaths,
                                         album_gain=True),
                           ref_levels)

  def test_tag(self):
    loudness = random.randint(-300, -1) / 10
    peak = random.randint(int(10 * loudness), -1) / 10
    ref_loudness_rg2 = -18
    expected_track_gain_rg2 = ref_loudness_rg2 - loudness
    expected_track_peak_rg2 = 10 ** (peak / 20)
    ref_loudness_opus = -23
    expected_track_gain_opus = ref_loudness_opus - loudness

    for i, delete_tags in zip(range(3), (False, True, False)):
      # i = 0 : add RG tag in existing tags
      # i = 1 : add RG tag with no existing tags
      # i = 2 : overwrites RG tag in existing tags
      with self.subTest(iteration=i + 1, delete_tags=delete_tags):
        if delete_tags:
          for file in (self.vorbis_filepath,
                       self.opus_filepath,
                       self.mp3_filepath,
                       self.m4a_filepath,
                       self.flac_filepath,
                       self.wv_filepath):
            self.assertEqual(r128gain.has_loudness_tag(file), (True, False))
            mf = mutagen.File(file)
            mf.delete()
            mf.save()
            self.assertEqual(r128gain.has_loudness_tag(file), (False, False))

        if delete_tags:
          mf = mutagen.File(self.vorbis_filepath)
          self.assertNotIn("REPLAYGAIN_TRACK_GAIN", mf)
          self.assertNotIn("REPLAYGAIN_TRACK_PEAK", mf)
        r128gain.tag(self.vorbis_filepath, loudness, peak)
        mf = mutagen.File(self.vorbis_filepath)
        self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
        self.assertIn("REPLAYGAIN_TRACK_GAIN", mf)
        self.assertEqual(mf["REPLAYGAIN_TRACK_GAIN"],
                         ["%.2f dB" % (expected_track_gain_rg2)])
        self.assertIn("REPLAYGAIN_TRACK_PEAK", mf)
        self.assertEqual(mf["REPLAYGAIN_TRACK_PEAK"], ["%.8f" % (expected_track_peak_rg2)])

        if delete_tags:
          mf = mutagen.File(self.opus_filepath)
          self.assertNotIn("R128_TRACK_GAIN", mf)
        r128gain.tag(self.opus_filepath, loudness, peak)
        mf = mutagen.File(self.opus_filepath)
        self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
        self.assertIn("R128_TRACK_GAIN", mf)
        self.assertEqual(mf["R128_TRACK_GAIN"], [str(int(round(expected_track_gain_opus * (2 ** 8), 0)))])

        if delete_tags:
          mf = mutagen.File(self.mp3_filepath)
          self.assertNotIn("TXXX:REPLAYGAIN_TRACK_GAIN", mf)
          self.assertNotIn("TXXX:REPLAYGAIN_TRACK_PEAK", mf)
        r128gain.tag(self.mp3_filepath, loudness, peak)
        mf = mutagen.File(self.mp3_filepath)
        self.assertIsInstance(mf.tags, mutagen.id3.ID3)
        self.assertIn("TXXX:REPLAYGAIN_TRACK_GAIN", mf)
        self.assertEqual(mf["TXXX:REPLAYGAIN_TRACK_GAIN"].text,
                         ["%.2f dB" % (expected_track_gain_rg2)])
        self.assertIn("TXXX:REPLAYGAIN_TRACK_PEAK", mf)
        self.assertEqual(mf["TXXX:REPLAYGAIN_TRACK_PEAK"].text, ["%.6f" % (expected_track_peak_rg2)])

        if delete_tags:
          mf = mutagen.File(self.m4a_filepath)
          self.assertNotIn("----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN", mf)
          self.assertNotIn("----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK", mf)
        r128gain.tag(self.m4a_filepath, loudness, peak)
        mf = mutagen.File(self.m4a_filepath)
        self.assertIsInstance(mf.tags, mutagen.mp4.MP4Tags)
        self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN", mf)
        self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN"]), 1)
        self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN"][0]).decode(),
                         "%.2f dB" % (expected_track_gain_rg2))
        self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK", mf)
        self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK"]), 1)
        self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK"][0]).decode(),
                         "%.6f" % (expected_track_peak_rg2))

        if delete_tags:
          mf = mutagen.File(self.flac_filepath)
          self.assertNotIn("REPLAYGAIN_TRACK_GAIN", mf)
          self.assertNotIn("REPLAYGAIN_TRACK_PEAK", mf)
        r128gain.tag(self.flac_filepath, loudness, peak)
        mf = mutagen.File(self.flac_filepath)
        self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
        self.assertIn("REPLAYGAIN_TRACK_GAIN", mf)
        self.assertEqual(mf["REPLAYGAIN_TRACK_GAIN"],
                         ["%.2f dB" % (expected_track_gain_rg2)])
        self.assertIn("REPLAYGAIN_TRACK_PEAK", mf)
        self.assertEqual(mf["REPLAYGAIN_TRACK_PEAK"], ["%.8f" % (expected_track_peak_rg2)])

        if delete_tags:
          mf = mutagen.File(self.wv_filepath)
          self.assertNotIn("REPLAYGAIN_TRACK_GAIN", mf)
          self.assertNotIn("REPLAYGAIN_TRACK_PEAK", mf)
        r128gain.tag(self.wv_filepath, loudness, peak)
        mf = mutagen.File(self.wv_filepath)
        self.assertIsInstance(mf.tags, mutagen.apev2.APEv2)
        self.assertIn("REPLAYGAIN_TRACK_GAIN", mf)
        self.assertEqual(str(mf["REPLAYGAIN_TRACK_GAIN"]),
                         "%.2f dB" % (expected_track_gain_rg2))
        self.assertIn("REPLAYGAIN_TRACK_PEAK", mf)
        self.assertEqual(str(mf["REPLAYGAIN_TRACK_PEAK"]), "%.8f" % (expected_track_peak_rg2))

        for file in (self.vorbis_filepath,
                     self.opus_filepath,
                     self.mp3_filepath,
                     self.m4a_filepath,
                     self.flac_filepath,
                     self.wv_filepath):
          self.assertEqual(r128gain.has_loudness_tag(file), (True, False))

  def test_process(self):
    ref_loudness_rg2 = -18
    ref_loudness_opus = -23

    for i, skip_tagged in enumerate((True, False, True)):

      if i == 0:
        for file in (self.vorbis_filepath,
                     self.opus_filepath,
                     self.mp3_filepath,
                     self.m4a_filepath,
                     self.flac_filepath,
                     self.wv_filepath):
          self.assertEqual(r128gain.has_loudness_tag(file), (False, False))

      for album_gain in (False, True):
        with self.subTest(i=i, skip_tagged=skip_tagged, album_gain=album_gain), \
                unittest.mock.patch("r128gain.get_r128_loudness", wraps=r128gain.get_r128_loudness) as get_r128_loudness_mock:

          r128gain.process((self.vorbis_filepath,
                            self.opus_filepath,
                            self.mp3_filepath,
                            self.m4a_filepath,
                            self.flac_filepath,
                            self.wv_filepath),
                           album_gain=album_gain,
                           skip_tagged=skip_tagged)

          if skip_tagged and (i > 0):
            self.assertEqual(get_r128_loudness_mock.call_count, 0)
          elif album_gain and skip_tagged:
            self.assertEqual(get_r128_loudness_mock.call_count, 1)
          elif album_gain and not skip_tagged:
            self.assertEqual(get_r128_loudness_mock.call_count, 7)
          else:
            self.assertEqual(get_r128_loudness_mock.call_count, 6)

          for file in (self.vorbis_filepath,
                       self.opus_filepath,
                       self.mp3_filepath,
                       self.m4a_filepath,
                       self.flac_filepath,
                       self.wv_filepath):
            self.assertEqual(r128gain.has_loudness_tag(file), (True, album_gain or (i > 0)))

          mf = mutagen.File(self.vorbis_filepath)
          self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
          self.assertIn("REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(mf["REPLAYGAIN_TRACK_GAIN"],
                           ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.vorbis_filepath][0])])
          self.assertIn("REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(mf["REPLAYGAIN_TRACK_PEAK"],
                           ["%.8f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20))])
          if album_gain:
            self.assertIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(mf["REPLAYGAIN_ALBUM_GAIN"],
                             ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0])])
            self.assertIn("REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(mf["REPLAYGAIN_ALBUM_PEAK"],
                             ["%.8f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20))])
          elif i == 0:
            self.assertNotIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("REPLAYGAIN_ALBUM_PEAK", mf)

          mf = mutagen.File(self.opus_filepath)
          self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
          self.assertIn("R128_TRACK_GAIN", mf)
          self.assertEqual(mf["R128_TRACK_GAIN"],
                           [str(r128gain.float_to_q7dot8(ref_loudness_opus - self.ref_levels[self.opus_filepath][0]))])
          if album_gain:
            self.assertIn("R128_ALBUM_GAIN", mf)
            self.assertEqual(mf["R128_ALBUM_GAIN"],
                             [str(r128gain.float_to_q7dot8(ref_loudness_opus - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0]))])
          elif i == 0:
            self.assertNotIn("R128_ALBUM_GAIN", mf)

          mf = mutagen.File(self.mp3_filepath)
          self.assertIsInstance(mf.tags, mutagen.id3.ID3)
          self.assertIn("TXXX:REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(mf["TXXX:REPLAYGAIN_TRACK_GAIN"].text,
                           ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.mp3_filepath][0])])
          self.assertIn("TXXX:REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(mf["TXXX:REPLAYGAIN_TRACK_PEAK"].text,
                           ["%.6f" % (10 ** (self.ref_levels[self.mp3_filepath][1] / 20))])
          if album_gain:
            self.assertIn("TXXX:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(mf["TXXX:REPLAYGAIN_ALBUM_GAIN"].text,
                             ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0])])
            self.assertIn("TXXX:REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(mf["TXXX:REPLAYGAIN_ALBUM_PEAK"].text,
                             ["%.6f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20))])
          elif i == 0:
            self.assertNotIn("TXXX:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("TXXX:REPLAYGAIN_ALBUM_GAIN", mf)

          mf = mutagen.File(self.m4a_filepath)
          self.assertIsInstance(mf.tags, mutagen.mp4.MP4Tags)
          self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN"]), 1)
          self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN"][0]).decode(),
                           "%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.m4a_filepath][0]))
          self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK"]), 1)
          self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK"][0]).decode(),
                           "1.011579")
          if album_gain:
            self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN"]), 1)
            self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN"][0]).decode(),
                             "%.2f dB" % (ref_loudness_rg2 - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0]))
            self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK"]), 1)
            self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK"][0]).decode(),
                             "%.6f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20)))
          elif i == 0:
            self.assertNotIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK", mf)

          mf = mutagen.File(self.flac_filepath)
          self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
          self.assertIn("REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(mf["REPLAYGAIN_TRACK_GAIN"],
                           ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.flac_filepath][0])])
          self.assertIn("REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(mf["REPLAYGAIN_TRACK_PEAK"], ["0.23173946"])
          if album_gain:
            self.assertIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(mf["REPLAYGAIN_ALBUM_GAIN"],
                             ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0])])
            self.assertIn("REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(mf["REPLAYGAIN_ALBUM_PEAK"],
                             ["%.8f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20))])
          elif i == 0:
            self.assertNotIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("REPLAYGAIN_ALBUM_PEAK", mf)

          mf = mutagen.File(self.wv_filepath)
          self.assertIsInstance(mf.tags, mutagen.apev2.APEv2)
          self.assertIn("REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(str(mf["REPLAYGAIN_TRACK_GAIN"]),
                           "%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.wv_filepath][0]))
          self.assertIn("REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(str(mf["REPLAYGAIN_TRACK_PEAK"]), "0.70794578")
          if album_gain:
            self.assertIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(str(mf["REPLAYGAIN_ALBUM_GAIN"]),
                             "%.2f dB" % (ref_loudness_rg2 - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0]))
            self.assertIn("REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(str(mf["REPLAYGAIN_ALBUM_PEAK"]),
                             "%.8f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20)))
          elif i == 0:
            self.assertNotIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("REPLAYGAIN_ALBUM_PEAK", mf)

  def test_process_recursive(self):
    ref_loudness_rg2 = -18
    ref_loudness_opus = -23

    album_dir1 = os.path.join(self.temp_dir.name, "a")
    os.mkdir(album_dir1)
    album1_vorbis_filepath = os.path.join(album_dir1,
                                          os.path.basename(self.vorbis_filepath))
    shutil.copyfile(self.vorbis_filepath, album1_vorbis_filepath)
    album1_opus_filepath = os.path.join(album_dir1,
                                        os.path.basename(self.opus_filepath))
    shutil.copyfile(self.opus_filepath, album1_opus_filepath)

    album_dir2 = os.path.join(self.temp_dir.name, "b")
    os.mkdir(album_dir2)
    album2_mp3_filepath = os.path.join(album_dir2,
                                       os.path.basename(self.mp3_filepath))
    shutil.copyfile(self.mp3_filepath, album2_mp3_filepath)
    album2_m4a_filepath = os.path.join(album_dir2,
                                       os.path.basename(self.m4a_filepath))
    shutil.copyfile(self.m4a_filepath, album2_m4a_filepath)
    album2_dummy_filepath = os.path.join(album_dir2, "dummy.txt")
    with open(album2_dummy_filepath, "wb") as f:
      f.write(b"\x00")

    ref_levels_dir1 = (-13, 2.6)
    ref_levels_dir2 = (-17.3, -0.5)

    # directory tree is as follows (root is self.temp_dir.name):
    # ├── a
    # │   ├── f.ogg
    # │   └── f.opus
    # ├── b
    # │   ├── dummy.txt
    # │   ├── f.m4a
    # │   └── f.mp3
    # ├── f.flac
    # ├── f.m4a
    # ├── f.mp3
    # ├── f.ogg
    # ├── f.opus
    # └── f.wv

    for i, skip_tagged in enumerate((True, False, True)):

      for album_gain in (False, True):
        with self.subTest(i=i, skip_tagged=skip_tagged, album_gain=album_gain), \
                unittest.mock.patch("r128gain.get_r128_loudness", wraps=r128gain.get_r128_loudness) as get_r128_loudness_mock:

          r128gain.process_recursive((self.temp_dir.name,),
                                     album_gain=album_gain,
                                     skip_tagged=skip_tagged)

          if skip_tagged and (i > 0):
            self.assertEqual(get_r128_loudness_mock.call_count, 0)
          elif album_gain and skip_tagged:
            self.assertEqual(get_r128_loudness_mock.call_count, 3)
          elif album_gain and not skip_tagged:
            self.assertEqual(get_r128_loudness_mock.call_count, 13)
          else:
            self.assertEqual(get_r128_loudness_mock.call_count, 10)

          # root tmp dir

          mf = mutagen.File(self.vorbis_filepath)
          self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
          self.assertIn("REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(mf["REPLAYGAIN_TRACK_GAIN"],
                           ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.vorbis_filepath][0])])
          self.assertIn("REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(mf["REPLAYGAIN_TRACK_PEAK"],
                           ["%.8f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20))])
          if album_gain:
            self.assertIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(mf["REPLAYGAIN_ALBUM_GAIN"],
                             ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0])])
            self.assertIn("REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(mf["REPLAYGAIN_ALBUM_PEAK"],
                             ["%.8f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20))])
          elif i == 0:
            self.assertNotIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("REPLAYGAIN_ALBUM_PEAK", mf)

          mf = mutagen.File(self.opus_filepath)
          self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
          self.assertIn("R128_TRACK_GAIN", mf)
          self.assertEqual(mf["R128_TRACK_GAIN"],
                           [str(r128gain.float_to_q7dot8(ref_loudness_opus - self.ref_levels[self.opus_filepath][0]))])
          if album_gain:
            self.assertIn("R128_ALBUM_GAIN", mf)
            self.assertEqual(mf["R128_ALBUM_GAIN"],
                             [str(r128gain.float_to_q7dot8(ref_loudness_opus - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0]))])
          elif i == 0:
            self.assertNotIn("R128_ALBUM_GAIN", mf)

          mf = mutagen.File(self.mp3_filepath)
          self.assertIsInstance(mf.tags, mutagen.id3.ID3)
          self.assertIn("TXXX:REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(mf["TXXX:REPLAYGAIN_TRACK_GAIN"].text,
                           ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.mp3_filepath][0])])
          self.assertIn("TXXX:REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(mf["TXXX:REPLAYGAIN_TRACK_PEAK"].text,
                           ["%.6f" % (10 ** (self.ref_levels[self.mp3_filepath][1] / 20))])
          if album_gain:
            self.assertIn("TXXX:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(mf["TXXX:REPLAYGAIN_ALBUM_GAIN"].text,
                             ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0])])
            self.assertIn("TXXX:REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(mf["TXXX:REPLAYGAIN_ALBUM_PEAK"].text,
                             ["%.6f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20))])
          elif i == 0:
            self.assertNotIn("TXXX:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("TXXX:REPLAYGAIN_ALBUM_GAIN", mf)

          mf = mutagen.File(self.m4a_filepath)
          self.assertIsInstance(mf.tags, mutagen.mp4.MP4Tags)
          self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN"]), 1)
          self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN"][0]).decode(),
                           "%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.m4a_filepath][0]))
          self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK"]), 1)
          self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK"][0]).decode(),
                           "1.011579")
          if album_gain:
            self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN"]), 1)
            self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN"][0]).decode(),
                             "%.2f dB" % (ref_loudness_rg2 - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0]))
            self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK"]), 1)
            self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK"][0]).decode(),
                             "%.6f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20)))
          elif i == 0:
            self.assertNotIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK", mf)

          mf = mutagen.File(self.flac_filepath)
          self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
          self.assertIn("REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(mf["REPLAYGAIN_TRACK_GAIN"],
                           ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.flac_filepath][0])])
          self.assertIn("REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(mf["REPLAYGAIN_TRACK_PEAK"], ["0.23173946"])
          if album_gain:
            self.assertIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(mf["REPLAYGAIN_ALBUM_GAIN"],
                             ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0])])
            self.assertIn("REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(mf["REPLAYGAIN_ALBUM_PEAK"],
                             ["%.8f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20))])
          elif i == 0:
            self.assertNotIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("REPLAYGAIN_ALBUM_PEAK", mf)

          mf = mutagen.File(self.wv_filepath)
          self.assertIsInstance(mf.tags, mutagen.apev2.APEv2)
          self.assertIn("REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(str(mf["REPLAYGAIN_TRACK_GAIN"]),
                           "%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.wv_filepath][0]))
          self.assertIn("REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(str(mf["REPLAYGAIN_TRACK_PEAK"]), "0.70794578")
          if album_gain:
            self.assertIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(str(mf["REPLAYGAIN_ALBUM_GAIN"]),
                             "%.2f dB" % (ref_loudness_rg2 - self.ref_levels[r128gain.ALBUM_GAIN_KEY][0]))
            self.assertIn("REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(str(mf["REPLAYGAIN_ALBUM_PEAK"]),
                             "%.8f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20)))
          elif i == 0:
            self.assertNotIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("REPLAYGAIN_ALBUM_PEAK", mf)

          # dir 1

          mf = mutagen.File(album1_vorbis_filepath)
          self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
          self.assertIn("REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(mf["REPLAYGAIN_TRACK_GAIN"],
                           ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.vorbis_filepath][0])])
          self.assertIn("REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(mf["REPLAYGAIN_TRACK_PEAK"],
                           ["%.8f" % (10 ** (self.ref_levels[self.max_peak_filepath][1] / 20))])
          if album_gain:
            self.assertIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(mf["REPLAYGAIN_ALBUM_GAIN"],
                             ["%.2f dB" % (ref_loudness_rg2 - ref_levels_dir1[0])])
            self.assertIn("REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(mf["REPLAYGAIN_ALBUM_PEAK"],
                             ["%.8f" % (10 ** (ref_levels_dir1[1] / 20))])
          elif i == 0:
            self.assertNotIn("REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("REPLAYGAIN_ALBUM_PEAK", mf)

          mf = mutagen.File(album1_opus_filepath)
          self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
          self.assertIn("R128_TRACK_GAIN", mf)
          self.assertEqual(mf["R128_TRACK_GAIN"],
                           [str(r128gain.float_to_q7dot8(ref_loudness_opus - self.ref_levels[self.opus_filepath][0]))])
          if album_gain:
            self.assertIn("R128_ALBUM_GAIN", mf)
            self.assertEqual(mf["R128_ALBUM_GAIN"],
                             [str(r128gain.float_to_q7dot8(ref_loudness_opus - ref_levels_dir1[0]))])
          elif i == 0:
            self.assertNotIn("R128_ALBUM_GAIN", mf)

          # dir 2

          mf = mutagen.File(album2_mp3_filepath)
          self.assertIsInstance(mf.tags, mutagen.id3.ID3)
          self.assertIn("TXXX:REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(mf["TXXX:REPLAYGAIN_TRACK_GAIN"].text,
                           ["%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.mp3_filepath][0])])
          self.assertIn("TXXX:REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(mf["TXXX:REPLAYGAIN_TRACK_PEAK"].text,
                           ["%.6f" % (10 ** (self.ref_levels[self.mp3_filepath][1] / 20))])
          if album_gain:
            self.assertIn("TXXX:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(mf["TXXX:REPLAYGAIN_ALBUM_GAIN"].text,
                             ["%.2f dB" % (ref_loudness_rg2 - ref_levels_dir2[0])])
            self.assertIn("TXXX:REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(mf["TXXX:REPLAYGAIN_ALBUM_PEAK"].text,
                             ["%.6f" % (10 ** (ref_levels_dir2[1] / 20))])
          elif i == 0:
            self.assertNotIn("TXXX:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("TXXX:REPLAYGAIN_ALBUM_GAIN", mf)

          mf = mutagen.File(album2_m4a_filepath)
          self.assertIsInstance(mf.tags, mutagen.mp4.MP4Tags)
          self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN", mf)
          self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN"]), 1)
          self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_GAIN"][0]).decode(),
                           "%.2f dB" % (ref_loudness_rg2 - self.ref_levels[self.m4a_filepath][0]))
          self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK", mf)
          self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK"]), 1)
          self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_TRACK_PEAK"][0]).decode(),
                           "1.011579")
          if album_gain:
            self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN"]), 1)
            self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN"][0]).decode(),
                             "%.2f dB" % (ref_loudness_rg2 - ref_levels_dir2[0]))
            self.assertIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK", mf)
            self.assertEqual(len(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK"]), 1)
            self.assertEqual(bytes(mf["----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK"][0]).decode(),
                             "%.6f" % (10 ** (ref_levels_dir2[1] / 20)))
          elif i == 0:
            self.assertNotIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_GAIN", mf)
            self.assertNotIn("----:COM.APPLE.ITUNES:REPLAYGAIN_ALBUM_PEAK", mf)

  def test_oggopus_output_gain(self):
    # non opus formats should not be parsed successfully
    with open(self.vorbis_filepath, "rb") as f:
      self.assertIsNone(r128gain.opusgain.parse_oggopus_output_gain(f))
    with open(self.mp3_filepath, "rb") as f:
      self.assertIsNone(r128gain.opusgain.parse_oggopus_output_gain(f))
    with open(self.m4a_filepath, "rb") as f:
      self.assertIsNone(r128gain.opusgain.parse_oggopus_output_gain(f))
    with open(self.flac_filepath, "rb") as f:
      self.assertIsNone(r128gain.opusgain.parse_oggopus_output_gain(f))
    with open(self.wv_filepath, "rb") as f:
      self.assertIsNone(r128gain.opusgain.parse_oggopus_output_gain(f))

    with open(self.opus_filepath, "r+b") as f:
      self.assertEqual(r128gain.opusgain.parse_oggopus_output_gain(f), 0)

      new_gain = random.randint(-32768, 32767)
      r128gain.opusgain.write_oggopus_output_gain(f, new_gain)

    with open(self.opus_filepath, "rb") as f:
      self.assertEqual(r128gain.opusgain.parse_oggopus_output_gain(f), new_gain)

  def test_tag_oggopus_output_gain(self):
    ref_loudness_opus = -23
    loudness = self.ref_levels[self.opus_filepath][0]
    r128gain.tag(self.opus_filepath, loudness, None, opus_output_gain=True)
    mf = mutagen.File(self.opus_filepath)
    self.assertIsInstance(mf.tags, mutagen._vorbis.VComment)
    self.assertIn("R128_TRACK_GAIN", mf)
    self.assertEqual(mf["R128_TRACK_GAIN"], ["0"])

    expected_output_gain = round((ref_loudness_opus - loudness) * (2 ** 8))
    with open(self.opus_filepath, "rb") as f:
      self.assertEqual(r128gain.opusgain.parse_oggopus_output_gain(f), expected_output_gain)

    self.assertEqual(r128gain.scan([self.opus_filepath]),
                     {self.opus_filepath: (float(ref_loudness_opus), None)})


if __name__ == "__main__":
  # disable logging
  logging.basicConfig(level=logging.CRITICAL + 1)
  #logging.basicConfig(level=logging.DEBUG)

  # run tests
  unittest.main()
