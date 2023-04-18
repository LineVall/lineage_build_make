#!/usr/bin/env python3
#
# Copyright (C) 2023 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Generate the SBOM of the current target product in SPDX format.
Usage example:
  generate-sbom.py --output_file out/target/product/vsoc_x86_64/sbom.spdx \
                   --metadata out/target/product/vsoc_x86_64/sbom-metadata.csv \
                   --product_out_dir=out/target/product/vsoc_x86_64 \
                   --build_version $(cat out/target/product/vsoc_x86_64/build_fingerprint.txt) \
                   --product_mfr=Google
"""

import argparse
import csv
import datetime
import google.protobuf.text_format as text_format
import hashlib
import os
import metadata_file_pb2
import sbom_data
import sbom_writers


# Package type
PKG_SOURCE = 'SOURCE'
PKG_UPSTREAM = 'UPSTREAM'
PKG_PREBUILT = 'PREBUILT'

# Security tag
NVD_CPE23 = 'NVD-CPE2.3:'

# Report
ISSUE_NO_METADATA = 'No metadata generated in Make for installed files:'
ISSUE_NO_METADATA_FILE = 'No METADATA file found for installed file:'
ISSUE_METADATA_FILE_INCOMPLETE = 'METADATA file incomplete:'
ISSUE_UNKNOWN_SECURITY_TAG_TYPE = 'Unknown security tag type:'
ISSUE_INSTALLED_FILE_NOT_EXIST = 'Non-exist installed files:'
INFO_METADATA_FOUND_FOR_PACKAGE = 'METADATA file found for packages:'


def get_args():
  parser = argparse.ArgumentParser()
  parser.add_argument('-v', '--verbose', action='store_true', default=False, help='Print more information.')
  parser.add_argument('--output_file', required=True, help='The generated SBOM file in SPDX format.')
  parser.add_argument('--metadata', required=True, help='The SBOM metadata file path.')
  parser.add_argument('--product_out_dir', required=True, help='The parent directory of all the installed files.')
  parser.add_argument('--build_version', required=True, help='The build version.')
  parser.add_argument('--product_mfr', required=True, help='The product manufacturer.')
  parser.add_argument('--json', action='store_true', default=False, help='Generated SBOM file in SPDX JSON format')
  parser.add_argument('--unbundled', action='store_true', default=False, help='Generate SBOM file for unbundled module')

  return parser.parse_args()


def log(*info):
  if args.verbose:
    for i in info:
      print(i)


def encode_for_spdxid(s):
  """Simple encode for string values used in SPDXID which uses the charset of A-Za-Z0-9.-"""
  result = ''
  for c in s:
    if c.isalnum() or c in '.-':
      result += c
    elif c in '_@/':
      result += '-'
    else:
      result += '0x' + c.encode('utf-8').hex()

  return result.lstrip('-')


def new_package_id(package_name, type):
  return f'SPDXRef-{type}-{encode_for_spdxid(package_name)}'


def new_file_id(file_path):
  return f'SPDXRef-{encode_for_spdxid(file_path)}'


def checksum(file_path):
  file_path = args.product_out_dir + '/' + file_path
  h = hashlib.sha1()
  if os.path.islink(file_path):
    h.update(os.readlink(file_path).encode('utf-8'))
  else:
    with open(file_path, 'rb') as f:
      h.update(f.read())
  return f'SHA1: {h.hexdigest()}'


def is_soong_prebuilt_module(file_metadata):
  return file_metadata['soong_module_type'] and file_metadata['soong_module_type'] in [
      'android_app_import', 'android_library_import', 'cc_prebuilt_binary', 'cc_prebuilt_library',
      'cc_prebuilt_library_headers', 'cc_prebuilt_library_shared', 'cc_prebuilt_library_static', 'cc_prebuilt_object',
      'dex_import', 'java_import', 'java_sdk_library_import', 'java_system_modules_import',
      'libclang_rt_prebuilt_library_static', 'libclang_rt_prebuilt_library_shared', 'llvm_prebuilt_library_static',
      'ndk_prebuilt_object', 'ndk_prebuilt_shared_stl', 'nkd_prebuilt_static_stl', 'prebuilt_apex',
      'prebuilt_bootclasspath_fragment', 'prebuilt_dsp', 'prebuilt_firmware', 'prebuilt_kernel_modules',
      'prebuilt_rfsa', 'prebuilt_root', 'rust_prebuilt_dylib', 'rust_prebuilt_library', 'rust_prebuilt_rlib',
      'vndk_prebuilt_shared',

      # 'android_test_import',
      # 'cc_prebuilt_test_library_shared',
      # 'java_import_host',
      # 'java_test_import',
      # 'llvm_host_prebuilt_library_shared',
      # 'prebuilt_apis',
      # 'prebuilt_build_tool',
      # 'prebuilt_defaults',
      # 'prebuilt_etc',
      # 'prebuilt_etc_host',
      # 'prebuilt_etc_xml',
      # 'prebuilt_font',
      # 'prebuilt_hidl_interfaces',
      # 'prebuilt_platform_compat_config',
      # 'prebuilt_stubs_sources',
      # 'prebuilt_usr_share',
      # 'prebuilt_usr_share_host',
      # 'soong_config_module_type_import',
  ]


def is_source_package(file_metadata):
  module_path = file_metadata['module_path']
  return module_path.startswith('external/') and not is_prebuilt_package(file_metadata)


def is_prebuilt_package(file_metadata):
  module_path = file_metadata['module_path']
  if module_path:
    return (module_path.startswith('prebuilts/') or
            is_soong_prebuilt_module(file_metadata) or
            file_metadata['is_prebuilt_make_module'])

  kernel_module_copy_files = file_metadata['kernel_module_copy_files']
  if kernel_module_copy_files and not kernel_module_copy_files.startswith('ANDROID-GEN:'):
    return True

  return False


def get_source_package_info(file_metadata, metadata_file_path):
  """Return source package info exists in its METADATA file, currently including name, security tag
  and external SBOM reference.

  See go/android-spdx and go/android-sbom-gen for more details.
  """
  if not metadata_file_path:
    return file_metadata['module_path'], []

  metadata_proto = metadata_file_protos[metadata_file_path]
  external_refs = []
  for tag in metadata_proto.third_party.security.tag:
    if tag.lower().startswith((NVD_CPE23 + 'cpe:2.3:').lower()):
      external_refs.append(
        sbom_data.PackageExternalRef(category=sbom_data.PackageExternalRefCategory.SECURITY,
                                     type=sbom_data.PackageExternalRefType.cpe23Type,
                                     locator=tag.removeprefix(NVD_CPE23)))
    elif tag.lower().startswith((NVD_CPE23 + 'cpe:/').lower()):
      external_refs.append(
        sbom_data.PackageExternalRef(category=sbom_data.PackageExternalRefCategory.SECURITY,
                                     type=sbom_data.PackageExternalRefType.cpe22Type,
                                     locator=tag.removeprefix(NVD_CPE23)))

  if metadata_proto.name:
    return metadata_proto.name, external_refs
  else:
    return os.path.basename(metadata_file_path), external_refs  # return the directory name only as package name


def get_prebuilt_package_name(file_metadata, metadata_file_path):
  """Return name of a prebuilt package, which can be from the METADATA file, metadata file path,
  module path or kernel module's source path if the installed file is a kernel module.

  See go/android-spdx and go/android-sbom-gen for more details.
  """
  name = None
  if metadata_file_path:
    metadata_proto = metadata_file_protos[metadata_file_path]
    if metadata_proto.name:
      name = metadata_proto.name
    else:
      name = metadata_file_path
  elif file_metadata['module_path']:
    name = file_metadata['module_path']
  elif file_metadata['kernel_module_copy_files']:
    src_path = file_metadata['kernel_module_copy_files'].split(':')[0]
    name = os.path.dirname(src_path)

  return name.removeprefix('prebuilts/').replace('/', '-')


def get_metadata_file_path(file_metadata):
  """Search for METADATA file of a package and return its path."""
  metadata_path = ''
  if file_metadata['module_path']:
    metadata_path = file_metadata['module_path']
  elif file_metadata['kernel_module_copy_files']:
    metadata_path = os.path.dirname(file_metadata['kernel_module_copy_files'].split(':')[0])

  while metadata_path and not os.path.exists(metadata_path + '/METADATA'):
    metadata_path = os.path.dirname(metadata_path)

  return metadata_path


def get_package_version(metadata_file_path):
  """Return a package's version in its METADATA file."""
  if not metadata_file_path:
    return None
  metadata_proto = metadata_file_protos[metadata_file_path]
  return metadata_proto.third_party.version


def get_package_homepage(metadata_file_path):
  """Return a package's homepage URL in its METADATA file."""
  if not metadata_file_path:
    return None
  metadata_proto = metadata_file_protos[metadata_file_path]
  if metadata_proto.third_party.homepage:
    return metadata_proto.third_party.homepage
  for url in metadata_proto.third_party.url:
    if url.type == metadata_file_pb2.URL.Type.HOMEPAGE:
      return url.value

  return None


def get_package_download_location(metadata_file_path):
  """Return a package's code repository URL in its METADATA file."""
  if not metadata_file_path:
    return None
  metadata_proto = metadata_file_protos[metadata_file_path]
  if metadata_proto.third_party.url:
    urls = sorted(metadata_proto.third_party.url, key=lambda url: url.type)
    if urls[0].type != metadata_file_pb2.URL.Type.HOMEPAGE:
      return urls[0].value
    elif len(urls) > 1:
      return urls[1].value

  return None


def get_sbom_fragments(installed_file_metadata, metadata_file_path):
  """Return SPDX fragment of source/prebuilt packages, which usually contains a SOURCE/PREBUILT
  package, a UPSTREAM package if it's a source package and a external SBOM document reference if
  it's a prebuilt package with sbom_ref defined in its METADATA file.

  See go/android-spdx and go/android-sbom-gen for more details.
  """
  external_doc_ref = None
  packages = []
  relationships = []

  # Info from METADATA file
  homepage = get_package_homepage(metadata_file_path)
  version = get_package_version(metadata_file_path)
  download_location = get_package_download_location(metadata_file_path)

  if is_source_package(installed_file_metadata):
    # Source fork packages
    name, external_refs = get_source_package_info(installed_file_metadata, metadata_file_path)
    source_package_id = new_package_id(name, PKG_SOURCE)
    source_package = sbom_data.Package(id=source_package_id, name=name, version=args.build_version,
                                       download_location=sbom_data.VALUE_NONE,
                                       supplier='Organization: ' + args.product_mfr,
                                       external_refs=external_refs)

    upstream_package_id = new_package_id(name, PKG_UPSTREAM)
    upstream_package = sbom_data.Package(id=upstream_package_id, name=name, version=version,
                                         supplier=('Organization: ' + homepage) if homepage else sbom_data.VALUE_NOASSERTION,
                                         download_location=download_location)
    packages += [source_package, upstream_package]
    relationships.append(sbom_data.Relationship(id1=source_package_id,
                                                relationship=sbom_data.RelationshipType.VARIANT_OF,
                                                id2=upstream_package_id))
  elif is_prebuilt_package(installed_file_metadata):
    # Prebuilt fork packages
    name = get_prebuilt_package_name(installed_file_metadata, metadata_file_path)
    prebuilt_package_id = new_package_id(name, PKG_PREBUILT)
    prebuilt_package = sbom_data.Package(id=prebuilt_package_id,
                                         name=name,
                                         download_location=sbom_data.VALUE_NONE,
                                         version=args.build_version,
                                         supplier='Organization: ' + args.product_mfr)
    packages.append(prebuilt_package)

    if metadata_file_path:
      metadata_proto = metadata_file_protos[metadata_file_path]
      if metadata_proto.third_party.WhichOneof('sbom') == 'sbom_ref':
        sbom_url = metadata_proto.third_party.sbom_ref.url
        sbom_checksum = metadata_proto.third_party.sbom_ref.checksum
        upstream_element_id = metadata_proto.third_party.sbom_ref.element_id
        if sbom_url and sbom_checksum and upstream_element_id:
          doc_ref_id = f'DocumentRef-{PKG_UPSTREAM}-{encode_for_spdxid(name)}'
          external_doc_ref = sbom_data.DocumentExternalReference(id=doc_ref_id,
                                                                 uri=sbom_url,
                                                                 checksum=sbom_checksum)
          relationships.append(
            sbom_data.Relationship(id1=prebuilt_package_id,
                                   relationship=sbom_data.RelationshipType.VARIANT_OF,
                                   id2=doc_ref_id + ':' + upstream_element_id))

  return external_doc_ref, packages, relationships


def generate_package_verification_code(files):
  checksums = [file.checksum for file in files]
  checksums.sort()
  h = hashlib.sha1()
  h.update(''.join(checksums).encode(encoding='utf-8'))
  return h.hexdigest()


def save_report(report):
  prefix, _ = os.path.splitext(args.output_file)
  with open(prefix + '-gen-report.txt', 'w', encoding='utf-8') as report_file:
    for type, issues in report.items():
      report_file.write(type + '\n')
      for issue in issues:
        report_file.write('\t' + issue + '\n')
      report_file.write('\n')


# Validate the metadata generated by Make for installed files and report if there is no metadata.
def installed_file_has_metadata(installed_file_metadata, report):
  installed_file = installed_file_metadata['installed_file']
  module_path = installed_file_metadata['module_path']
  product_copy_files = installed_file_metadata['product_copy_files']
  kernel_module_copy_files = installed_file_metadata['kernel_module_copy_files']
  is_platform_generated = installed_file_metadata['is_platform_generated']

  if (not module_path and
      not product_copy_files and
      not kernel_module_copy_files and
      not is_platform_generated and
      not installed_file.endswith('.fsv_meta')):
    report[ISSUE_NO_METADATA].append(installed_file)
    return False

  return True


def report_metadata_file(metadata_file_path, installed_file_metadata, report):
  if metadata_file_path:
    report[INFO_METADATA_FOUND_FOR_PACKAGE].append(
        'installed_file: {}, module_path: {}, METADATA file: {}'.format(
            installed_file_metadata['installed_file'],
            installed_file_metadata['module_path'],
            metadata_file_path + '/METADATA'))

    package_metadata = metadata_file_pb2.Metadata()
    with open(metadata_file_path + '/METADATA', 'rt') as f:
      text_format.Parse(f.read(), package_metadata)

    if not metadata_file_path in metadata_file_protos:
      metadata_file_protos[metadata_file_path] = package_metadata
      if not package_metadata.name:
        report[ISSUE_METADATA_FILE_INCOMPLETE].append(f'{metadata_file_path}/METADATA does not has "name"')

      if not package_metadata.third_party.version:
        report[ISSUE_METADATA_FILE_INCOMPLETE].append(
            f'{metadata_file_path}/METADATA does not has "third_party.version"')

      for tag in package_metadata.third_party.security.tag:
        if not tag.startswith(NVD_CPE23):
          report[ISSUE_UNKNOWN_SECURITY_TAG_TYPE].append(
              f'Unknown security tag type: {tag} in {metadata_file_path}/METADATA')
  else:
    report[ISSUE_NO_METADATA_FILE].append(
        "installed_file: {}, module_path: {}".format(
            installed_file_metadata['installed_file'], installed_file_metadata['module_path']))


def generate_sbom_for_unbundled():
  with open(args.metadata, newline='') as sbom_metadata_file:
    reader = csv.DictReader(sbom_metadata_file)
    doc = sbom_data.Document(name=args.build_version,
                             namespace=f'https://www.google.com/sbom/spdx/android/{args.build_version}',
                             creators=['Organization: ' + args.product_mfr])
    for installed_file_metadata in reader:
      installed_file = installed_file_metadata['installed_file']
      if args.output_file != args.product_out_dir + installed_file + ".spdx":
        continue

      module_path = installed_file_metadata['module_path']
      package_id = new_package_id(module_path, PKG_PREBUILT)
      package = sbom_data.Package(id=package_id,
                                  name=module_path,
                                  version=args.build_version,
                                  supplier='Organization: ' + args.product_mfr)
      file_id = new_file_id(installed_file)
      file = sbom_data.File(id=file_id, name=installed_file, checksum=checksum(installed_file))
      relationship = sbom_data.Relationship(id1=file_id,
                                            relationship=sbom_data.RelationshipType.GENERATED_FROM,
                                            id2=package_id)
      doc.add_package(package)
      doc.files.append(file)
      doc.describes = file_id
      doc.add_relationship(relationship)
      doc.created = datetime.datetime.now(tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
      break

  with open(args.output_file, 'w', encoding="utf-8") as file:
    sbom_writers.TagValueWriter.write(doc, file, fragment=True)


def main():
  global args
  args = get_args()
  log('Args:', vars(args))

  if args.unbundled:
    generate_sbom_for_unbundled()
    return

  global metadata_file_protos
  metadata_file_protos = {}

  doc = sbom_data.Document(name=args.build_version,
                           namespace=f'https://www.google.com/sbom/spdx/android/{args.build_version}',
                           creators=['Organization: ' + args.product_mfr])

  product_package = sbom_data.Package(id=sbom_data.SPDXID_PRODUCT,
                                      name=sbom_data.PACKAGE_NAME_PRODUCT,
                                      download_location=sbom_data.VALUE_NONE,
                                      version=args.build_version,
                                      supplier='Organization: ' + args.product_mfr,
                                      files_analyzed=True)
  doc.packages.append(product_package)

  doc.packages.append(sbom_data.Package(id=sbom_data.SPDXID_PLATFORM,
                                        name=sbom_data.PACKAGE_NAME_PLATFORM,
                                        download_location=sbom_data.VALUE_NONE,
                                        version=args.build_version,
                                        supplier='Organization: ' + args.product_mfr))

  # Report on some issues and information
  report = {
    ISSUE_NO_METADATA: [],
    ISSUE_NO_METADATA_FILE: [],
    ISSUE_METADATA_FILE_INCOMPLETE: [],
    ISSUE_UNKNOWN_SECURITY_TAG_TYPE: [],
    ISSUE_INSTALLED_FILE_NOT_EXIST: [],
    INFO_METADATA_FOUND_FOR_PACKAGE: [],
  }

  # Scan the metadata in CSV file and create the corresponding package and file records in SPDX
  with open(args.metadata, newline='') as sbom_metadata_file:
    reader = csv.DictReader(sbom_metadata_file)
    for installed_file_metadata in reader:
      installed_file = installed_file_metadata['installed_file']
      module_path = installed_file_metadata['module_path']
      product_copy_files = installed_file_metadata['product_copy_files']
      kernel_module_copy_files = installed_file_metadata['kernel_module_copy_files']

      if not installed_file_has_metadata(installed_file_metadata, report):
        continue
      file_path = args.product_out_dir + '/' + installed_file
      if not (os.path.islink(file_path) or os.path.isfile(file_path)):
        report[ISSUE_INSTALLED_FILE_NOT_EXIST].append(installed_file)
        continue

      file_id = new_file_id(installed_file)
      doc.files.append(
        sbom_data.File(id=file_id, name=installed_file, checksum=checksum(installed_file)))
      product_package.file_ids.append(file_id)

      if is_source_package(installed_file_metadata) or is_prebuilt_package(installed_file_metadata):
        metadata_file_path = get_metadata_file_path(installed_file_metadata)
        report_metadata_file(metadata_file_path, installed_file_metadata, report)

        # File from source fork packages or prebuilt fork packages
        external_doc_ref, pkgs, rels = get_sbom_fragments(installed_file_metadata, metadata_file_path)
        if len(pkgs) > 0:
          if external_doc_ref:
            doc.add_external_ref(external_doc_ref)
          for p in pkgs:
            doc.add_package(p)
          for rel in rels:
            doc.add_relationship(rel)
          fork_package_id = pkgs[0].id  # The first package should be the source/prebuilt fork package
          doc.add_relationship(sbom_data.Relationship(id1=file_id,
                                                      relationship=sbom_data.RelationshipType.GENERATED_FROM,
                                                      id2=fork_package_id))
      elif module_path or installed_file_metadata['is_platform_generated']:
        # File from PLATFORM package
        doc.add_relationship(sbom_data.Relationship(id1=file_id,
                                                    relationship=sbom_data.RelationshipType.GENERATED_FROM,
                                                    id2=sbom_data.SPDXID_PLATFORM))
      elif product_copy_files:
        # Format of product_copy_files: <source path>:<dest path>
        src_path = product_copy_files.split(':')[0]
        # So far product_copy_files are copied from directory system, kernel, hardware, frameworks and device,
        # so process them as files from PLATFORM package
        doc.add_relationship(sbom_data.Relationship(id1=file_id,
                                                    relationship=sbom_data.RelationshipType.GENERATED_FROM,
                                                    id2=sbom_data.SPDXID_PLATFORM))
      elif installed_file.endswith('.fsv_meta'):
        # See build/make/core/Makefile:2988
        doc.add_relationship(sbom_data.Relationship(id1=file_id,
                                                    relationship=sbom_data.RelationshipType.GENERATED_FROM,
                                                    id2=sbom_data.SPDXID_PLATFORM))
      elif kernel_module_copy_files.startswith('ANDROID-GEN'):
        # For the four files generated for _dlkm, _ramdisk partitions
        # See build/make/core/Makefile:323
        doc.add_relationship(sbom_data.Relationship(id1=file_id,
                                                    relationship=sbom_data.RelationshipType.GENERATED_FROM,
                                                    id2=sbom_data.SPDXID_PLATFORM))

  product_package.verification_code = generate_package_verification_code(doc.files)

  # Save SBOM records to output file
  doc.created = datetime.datetime.now(tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
  with open(args.output_file, 'w', encoding="utf-8") as file:
    sbom_writers.TagValueWriter.write(doc, file)
  if args.json:
    with open(args.output_file+'.json', 'w', encoding="utf-8") as file:
      sbom_writers.JSONWriter.write(doc, file)


if __name__ == '__main__':
  main()