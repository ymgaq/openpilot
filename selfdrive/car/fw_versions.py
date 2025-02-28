#!/usr/bin/env python3
import struct
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set, Tuple
from tqdm import tqdm

import panda.python.uds as uds
from cereal import car
from selfdrive.car.ecu_addrs import get_ecu_addrs
from selfdrive.car.interfaces import get_interface_attr
from selfdrive.car.fingerprints import FW_VERSIONS
from selfdrive.car.isotp_parallel_query import IsoTpParallelQuery
from selfdrive.car.toyota.values import CAR as TOYOTA
from system.swaglog import cloudlog

Ecu = car.CarParams.Ecu
ESSENTIAL_ECUS = [Ecu.engine, Ecu.eps, Ecu.esp, Ecu.fwdRadar, Ecu.fwdCamera, Ecu.vsa]


def p16(val):
  return struct.pack("!H", val)


TESTER_PRESENT_REQUEST = bytes([uds.SERVICE_TYPE.TESTER_PRESENT, 0x0])
TESTER_PRESENT_RESPONSE = bytes([uds.SERVICE_TYPE.TESTER_PRESENT + 0x40, 0x0])

SHORT_TESTER_PRESENT_REQUEST = bytes([uds.SERVICE_TYPE.TESTER_PRESENT])
SHORT_TESTER_PRESENT_RESPONSE = bytes([uds.SERVICE_TYPE.TESTER_PRESENT + 0x40])

DEFAULT_DIAGNOSTIC_REQUEST = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL,
                                    uds.SESSION_TYPE.DEFAULT])
DEFAULT_DIAGNOSTIC_RESPONSE = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL + 0x40,
                                    uds.SESSION_TYPE.DEFAULT, 0x0, 0x32, 0x1, 0xf4])

EXTENDED_DIAGNOSTIC_REQUEST = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL,
                                     uds.SESSION_TYPE.EXTENDED_DIAGNOSTIC])
EXTENDED_DIAGNOSTIC_RESPONSE = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL + 0x40,
                                      uds.SESSION_TYPE.EXTENDED_DIAGNOSTIC, 0x0, 0x32, 0x1, 0xf4])

UDS_VERSION_REQUEST = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.APPLICATION_SOFTWARE_IDENTIFICATION)
UDS_VERSION_RESPONSE = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER + 0x40]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.APPLICATION_SOFTWARE_IDENTIFICATION)


HYUNDAI_VERSION_REQUEST_LONG = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(0xf100)  # Long description
HYUNDAI_VERSION_REQUEST_MULTI = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.VEHICLE_MANUFACTURER_SPARE_PART_NUMBER) + \
  p16(uds.DATA_IDENTIFIER_TYPE.APPLICATION_SOFTWARE_IDENTIFICATION) + \
  p16(0xf100)
HYUNDAI_VERSION_RESPONSE = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER + 0x40])


TOYOTA_VERSION_REQUEST = b'\x1a\x88\x01'
TOYOTA_VERSION_RESPONSE = b'\x5a\x88\x01'

VOLKSWAGEN_VERSION_REQUEST_MULTI = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.VEHICLE_MANUFACTURER_SPARE_PART_NUMBER) + \
  p16(uds.DATA_IDENTIFIER_TYPE.VEHICLE_MANUFACTURER_ECU_SOFTWARE_VERSION_NUMBER) + \
  p16(uds.DATA_IDENTIFIER_TYPE.APPLICATION_DATA_IDENTIFICATION)
VOLKSWAGEN_VERSION_RESPONSE = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER + 0x40])

OBD_VERSION_REQUEST = b'\x09\x04'
OBD_VERSION_RESPONSE = b'\x49\x04'

DEFAULT_RX_OFFSET = 0x8
VOLKSWAGEN_RX_OFFSET = 0x6a

MAZDA_VERSION_REQUEST = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.VEHICLE_MANUFACTURER_ECU_SOFTWARE_NUMBER)
MAZDA_VERSION_RESPONSE =  bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER + 0x40]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.VEHICLE_MANUFACTURER_ECU_SOFTWARE_NUMBER)

NISSAN_DIAGNOSTIC_REQUEST_KWP = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL, 0xc0])
NISSAN_DIAGNOSTIC_RESPONSE_KWP = bytes([uds.SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL + 0x40, 0xc0])

NISSAN_VERSION_REQUEST_KWP = b'\x21\x83'
NISSAN_VERSION_RESPONSE_KWP = b'\x61\x83'

NISSAN_VERSION_REQUEST_STANDARD = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.VEHICLE_MANUFACTURER_ECU_SOFTWARE_NUMBER)
NISSAN_VERSION_RESPONSE_STANDARD =  bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER + 0x40]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.VEHICLE_MANUFACTURER_ECU_SOFTWARE_NUMBER)

NISSAN_RX_OFFSET = 0x20

SUBARU_VERSION_REQUEST = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.APPLICATION_DATA_IDENTIFICATION)
SUBARU_VERSION_RESPONSE = bytes([uds.SERVICE_TYPE.READ_DATA_BY_IDENTIFIER + 0x40]) + \
  p16(uds.DATA_IDENTIFIER_TYPE.APPLICATION_DATA_IDENTIFICATION)


@dataclass
class Request:
  brand: str
  request: List[bytes]
  response: List[bytes]
  whitelist_ecus: List[int] = field(default_factory=list)
  rx_offset: int = DEFAULT_RX_OFFSET
  bus: int = 1


REQUESTS: List[Request] = [
  # Subaru
  Request(
    "subaru",
    [TESTER_PRESENT_REQUEST, SUBARU_VERSION_REQUEST],
    [TESTER_PRESENT_RESPONSE, SUBARU_VERSION_RESPONSE],
  ),
  # Hyundai
  Request(
    "hyundai",
    [HYUNDAI_VERSION_REQUEST_LONG],
    [HYUNDAI_VERSION_RESPONSE],
  ),
  Request(
    "hyundai",
    [HYUNDAI_VERSION_REQUEST_MULTI],
    [HYUNDAI_VERSION_RESPONSE],
  ),
  # Honda
  Request(
    "honda",
    [UDS_VERSION_REQUEST],
    [UDS_VERSION_RESPONSE],
  ),
  # Toyota
  Request(
    "toyota",
    [SHORT_TESTER_PRESENT_REQUEST, TOYOTA_VERSION_REQUEST],
    [SHORT_TESTER_PRESENT_RESPONSE, TOYOTA_VERSION_RESPONSE],
  ),
  Request(
    "toyota",
    [SHORT_TESTER_PRESENT_REQUEST, OBD_VERSION_REQUEST],
    [SHORT_TESTER_PRESENT_RESPONSE, OBD_VERSION_RESPONSE],
  ),
  Request(
    "toyota",
    [TESTER_PRESENT_REQUEST, DEFAULT_DIAGNOSTIC_REQUEST, EXTENDED_DIAGNOSTIC_REQUEST, UDS_VERSION_REQUEST],
    [TESTER_PRESENT_RESPONSE, DEFAULT_DIAGNOSTIC_RESPONSE, EXTENDED_DIAGNOSTIC_RESPONSE, UDS_VERSION_RESPONSE],
  ),
  # Volkswagen
  Request(
    "volkswagen",
    [VOLKSWAGEN_VERSION_REQUEST_MULTI],
    [VOLKSWAGEN_VERSION_RESPONSE],
    whitelist_ecus=[Ecu.srs, Ecu.eps, Ecu.fwdRadar],
    rx_offset=VOLKSWAGEN_RX_OFFSET,
  ),
  Request(
    "volkswagen",
    [VOLKSWAGEN_VERSION_REQUEST_MULTI],
    [VOLKSWAGEN_VERSION_RESPONSE],
    whitelist_ecus=[Ecu.engine, Ecu.transmission],
  ),
  # Mazda
  Request(
    "mazda",
    [MAZDA_VERSION_REQUEST],
    [MAZDA_VERSION_RESPONSE],
  ),
  # Nissan
  Request(
    "nissan",
    [NISSAN_DIAGNOSTIC_REQUEST_KWP, NISSAN_VERSION_REQUEST_KWP],
    [NISSAN_DIAGNOSTIC_RESPONSE_KWP, NISSAN_VERSION_RESPONSE_KWP],
  ),
  Request(
    "nissan",
    [NISSAN_DIAGNOSTIC_REQUEST_KWP, NISSAN_VERSION_REQUEST_KWP],
    [NISSAN_DIAGNOSTIC_RESPONSE_KWP, NISSAN_VERSION_RESPONSE_KWP],
    rx_offset=NISSAN_RX_OFFSET,
  ),
  Request(
    "nissan",
    [NISSAN_VERSION_REQUEST_STANDARD],
    [NISSAN_VERSION_RESPONSE_STANDARD],
    rx_offset=NISSAN_RX_OFFSET,
  ),
  # Body
  Request(
    "body",
    [TESTER_PRESENT_REQUEST, UDS_VERSION_REQUEST],
    [TESTER_PRESENT_RESPONSE, UDS_VERSION_RESPONSE],
    bus=0,
  ),
]


def chunks(l, n=128):
  for i in range(0, len(l), n):
    yield l[i:i + n]


def build_fw_dict(fw_versions):
  fw_versions_dict = {}
  for fw in fw_versions:
    addr = fw.address
    sub_addr = fw.subAddress if fw.subAddress != 0 else None
    fw_versions_dict[(addr, sub_addr)] = fw.fwVersion
  return fw_versions_dict


def match_fw_to_car_fuzzy(fw_versions_dict, log=True, exclude=None):
  """Do a fuzzy FW match. This function will return a match, and the number of firmware version
  that were matched uniquely to that specific car. If multiple ECUs uniquely match to different cars
  the match is rejected."""

  # These ECUs are known to be shared between models (EPS only between hybrid/ICE version)
  # Getting this exactly right isn't crucial, but excluding camera and radar makes it almost
  # impossible to get 3 matching versions, even if two models with shared parts are released at the same
  # time and only one is in our database.
  exclude_types = [Ecu.fwdCamera, Ecu.fwdRadar, Ecu.eps, Ecu.debug]

  # Build lookup table from (addr, subaddr, fw) to list of candidate cars
  all_fw_versions = defaultdict(list)
  for candidate, fw_by_addr in FW_VERSIONS.items():
    if candidate == exclude:
      continue

    for addr, fws in fw_by_addr.items():
      if addr[0] in exclude_types:
        continue
      for f in fws:
        all_fw_versions[(addr[1], addr[2], f)].append(candidate)

  match_count = 0
  candidate = None
  for addr, version in fw_versions_dict.items():
    # All cars that have this FW response on the specified address
    candidates = all_fw_versions[(addr[0], addr[1], version)]

    if len(candidates) == 1:
      match_count += 1
      if candidate is None:
        candidate = candidates[0]
      # We uniquely matched two different cars. No fuzzy match possible
      elif candidate != candidates[0]:
        return set()

  if match_count >= 2:
    if log:
      cloudlog.error(f"Fingerprinted {candidate} using fuzzy match. {match_count} matching ECUs")
    return {candidate}
  else:
    return set()


def match_fw_to_car_exact(fw_versions_dict):
  """Do an exact FW match. Returns all cars that match the given
  FW versions for a list of "essential" ECUs. If an ECU is not considered
  essential the FW version can be missing to get a fingerprint, but if it's present it
  needs to match the database."""
  invalid = []
  candidates = FW_VERSIONS

  for candidate, fws in candidates.items():
    for ecu, expected_versions in fws.items():
      ecu_type = ecu[0]
      addr = ecu[1:]
      found_version = fw_versions_dict.get(addr, None)
      if ecu_type == Ecu.esp and candidate in (TOYOTA.RAV4, TOYOTA.COROLLA, TOYOTA.HIGHLANDER, TOYOTA.SIENNA, TOYOTA.LEXUS_IS) and found_version is None:
        continue

      # On some Toyota models, the engine can show on two different addresses
      if ecu_type == Ecu.engine and candidate in (TOYOTA.CAMRY, TOYOTA.COROLLA_TSS2, TOYOTA.CHR, TOYOTA.LEXUS_IS) and found_version is None:
        continue

      # Ignore non essential ecus
      if ecu_type not in ESSENTIAL_ECUS and found_version is None:
        continue

      # Virtual debug ecu doesn't need to match the database
      if ecu_type == Ecu.debug:
        continue

      if found_version not in expected_versions:
        invalid.append(candidate)
        break

  return set(candidates.keys()) - set(invalid)


def match_fw_to_car(fw_versions, allow_fuzzy=True):
  fw_versions_dict = build_fw_dict(fw_versions)
  matches = match_fw_to_car_exact(fw_versions_dict)

  exact_match = True
  if allow_fuzzy and len(matches) == 0:
    matches = match_fw_to_car_fuzzy(fw_versions_dict)

    # Fuzzy match found
    if len(matches) == 1:
      exact_match = False

  return exact_match, matches


def get_present_ecus(logcan, sendcan):
  queries = list()
  parallel_queries = list()
  responses = set()
  versions = get_interface_attr('FW_VERSIONS', ignore_none=True)

  for r in REQUESTS:
    if r.brand not in versions:
      continue

    for brand_versions in versions[r.brand].values():
      for ecu_type, addr, sub_addr in brand_versions:
        # Only query ecus in whitelist if whitelist is not empty
        if len(r.whitelist_ecus) == 0 or ecu_type in r.whitelist_ecus:
          a = (addr, sub_addr, r.bus)
          # Build set of queries
          if sub_addr is None:
            if a not in parallel_queries:
              parallel_queries.append(a)
          else:  # subaddresses must be queried one by one
            if [a] not in queries:
              queries.append([a])

          # Build set of expected responses to filter
          response_addr = uds.get_rx_addr_for_tx_addr(addr, r.rx_offset)
          responses.add((response_addr, sub_addr, r.bus))

  queries.insert(0, parallel_queries)

  ecu_responses: Set[Tuple[int, Optional[int], int]] = set()
  for query in queries:
    ecu_responses.update(get_ecu_addrs(logcan, sendcan, set(query), responses, timeout=0.1))
  return ecu_responses


def get_fw_versions(logcan, sendcan, extra=None, timeout=0.1, debug=False, progress=False):
  ecu_types = {}

  # Extract ECU addresses to query from fingerprints
  # ECUs using a subaddress need be queried one by one, the rest can be done in parallel
  addrs = []
  parallel_addrs = []

  versions = get_interface_attr('FW_VERSIONS', ignore_none=True)
  if extra is not None:
    versions.update(extra)

  for brand, brand_versions in versions.items():
    for c in brand_versions.values():
      for ecu_type, addr, sub_addr in c.keys():
        a = (brand, addr, sub_addr)
        if a not in ecu_types:
          ecu_types[(addr, sub_addr)] = ecu_type

        if sub_addr is None:
          if a not in parallel_addrs:
            parallel_addrs.append(a)
        else:
          if [a] not in addrs:
            addrs.append([a])

  addrs.insert(0, parallel_addrs)

  fw_versions = {}
  for i, addr in enumerate(tqdm(addrs, disable=not progress)):
    for addr_chunk in chunks(addr):
      for r in REQUESTS:
        try:
          addrs = [(a, s) for (b, a, s) in addr_chunk if b in (r.brand, 'any') and
                   (len(r.whitelist_ecus) == 0 or ecu_types[(a, s)] in r.whitelist_ecus)]

          if addrs:
            query = IsoTpParallelQuery(sendcan, logcan, r.bus, addrs, r.request, r.response, r.rx_offset, debug=debug)
            t = 2 * timeout if i == 0 else timeout
            fw_versions.update({addr: (version, r.request, r.rx_offset) for addr, version in query.get_data(t).items()})
        except Exception:
          cloudlog.warning(f"FW query exception: {traceback.format_exc()}")

  # Build capnp list to put into CarParams
  car_fw = []
  for addr, (version, request, rx_offset) in fw_versions.items():
    f = car.CarParams.CarFw.new_message()

    f.ecu = ecu_types[addr]
    f.fwVersion = version
    f.address = addr[0]
    f.responseAddress = uds.get_rx_addr_for_tx_addr(addr[0], rx_offset)
    f.request = request

    if addr[1] is not None:
      f.subAddress = addr[1]

    car_fw.append(f)

  return car_fw


if __name__ == "__main__":
  import time
  import argparse
  import cereal.messaging as messaging
  from selfdrive.car.vin import get_vin

  parser = argparse.ArgumentParser(description='Get firmware version of ECUs')
  parser.add_argument('--scan', action='store_true')
  parser.add_argument('--debug', action='store_true')
  args = parser.parse_args()

  logcan = messaging.sub_sock('can')
  sendcan = messaging.pub_sock('sendcan')

  extra: Any = None
  if args.scan:
    extra = {}
    # Honda
    for i in range(256):
      extra[(Ecu.unknown, 0x18da00f1 + (i << 8), None)] = []
      extra[(Ecu.unknown, 0x700 + i, None)] = []
      extra[(Ecu.unknown, 0x750, i)] = []
    extra = {"any": {"debug": extra}}

  time.sleep(1.)

  t = time.time()
  print("Getting vin...")
  addr, vin = get_vin(logcan, sendcan, 1, retry=10, debug=args.debug)
  print(f"VIN: {vin}")
  print(f"Getting VIN took {time.time() - t:.3f} s")
  print()

  t = time.time()
  fw_vers = get_fw_versions(logcan, sendcan, extra=extra, debug=args.debug, progress=True)
  _, candidates = match_fw_to_car(fw_vers)

  print()
  print("Found FW versions")
  print("{")
  for version in fw_vers:
    subaddr = None if version.subAddress == 0 else hex(version.subAddress)
    print(f"  (Ecu.{version.ecu}, {hex(version.address)}, {subaddr}): [{version.fwVersion}]")
  print("}")

  print()
  print("Possible matches:", candidates)
  print(f"Getting fw took {time.time() - t:.3f} s")
