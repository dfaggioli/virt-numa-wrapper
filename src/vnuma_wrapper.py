#!/usr/bin/env python3
#
# SPDX-License-Identifier: GPL-2.0-only
#
# Copyright (C) 2026 Dario Faggioli
# Copyright (C) 2026 SUSE LLC
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
### DISCLAIMER: Proof of Concept ###
#
# This software is provided purely as a demonstrative tool and as a
# proof-of-concept. Its main purpose is to illustrate how dynamically
# calculating and injecting a Virtual NUMA (vNUMA) topology into a KVM/QEMU
# virtual machine XML configuration, basing on automatic NUMA pre-placement on
# the host hardware, can be done.
#
# The primary goal is to offer technical insights and, maybe, a reference for
# developers aiming at adding such a feature into their own orchestration
# platforms, middleweres, management stacks, etc.
#
# This project is not actively maintained for production use. There is no
# guarantee of correctness, integrity, security or ongoing support.
#
# Use it at your own risk!
#
### DISCLAIMER: Proof of Concept ###

import sys
import os
import subprocess
import tempfile
import re

try:
    from lxml import etree as ET
except ImportError:
    print("Error: lxml module not found.", file=sys.stderr)
    sys.exit(1)

def get_sysfs_cpulist(node_id):
    """
    Retrieves the physical CPU core mask associated with a specific host NUMA
    node directly from the kernel sysfs interface.
    """
    path = f"/sys/devices/system/node/node{node_id}/cpulist"
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "0"

def parse_nodeset(nstr):
    """
    Expands a node range string (e.g., '0,2-4') into a flat list of discrete
    integer node IDs.
    """
    nodes = []
    for part in nstr.split(","):
        if "-" in part:
            start, end = map(int, part.split("-"))
            nodes.extend(range(start, end + 1))
        else:
            nodes.append(int(part))
    return nodes

def indent_node(elem, level=1):
    """
    Applies structural whitespace formatting to newly instantiated lxml
    ElementTree nodes to guarantee 'diff-minimal', human-readable XML output.
    """
    indentation = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indentation + "  "
        for child in elem:
            indent_node(child, level + 1)
            if not child.tail or not child.tail.strip():
                child.tail = indentation + "  "
        if not child.tail or not child.tail.strip():
            child.tail = indentation
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indentation

def extract_vm_params(root):
    """
    Parses the domain tree of the provided XML config file to identify core
    virtual hardware requirements (vCPUs, RAM size) and normalizes existing
    HugePages configurations by stripping legacy guest node affinities.
    """
    vcpu_elem = root.find("./vcpu")
    mem_elem = root.find("./memory")
    if vcpu_elem is None or mem_elem is None:
        raise ValueError("Missing <vcpu> or <memory> in XML")
    
    vcpus = int(vcpu_elem.text)
    mem_kib = int(mem_elem.text)
    
    hp_size_mb = 0
    mem_backing = root.find("./memoryBacking")
    if mem_backing is not None:
        hugepages = mem_backing.find("./hugepages")
        if hugepages is not None:
            page = hugepages.find("./page")
            if page is not None:
                if "nodeset" in page.attrib:
                    del page.attrib["nodeset"]
                p_size = int(page.get("size", "2048"))
                p_unit = page.get("unit", "KiB")
                
                if p_unit == "KiB": hp_size_mb = p_size // 1024
                elif p_unit == "MiB": hp_size_mb = p_size
                elif p_unit == "GiB": hp_size_mb = p_size * 1024
                elif p_unit in ("bytes", "B"): hp_size_mb = p_size // (1024 * 1024)
                else: hp_size_mb = p_size // 1024
            else:
                hp_size_mb = 2
                
    return vcpus, mem_kib, hp_size_mb

def inject_topology(root, pnuma_str, sysfs_mapper_func):
    """
    Injects an ACPI MADT/SRAT-compliant virtual topology into the XML.
    Enforces a deterministic mapping between virtual sockets and vNUMA
    cells, and establishes strict host-level pinning for both memory
    and vCPUs.
    """
    vcpus, mem_kib, _ = extract_vm_params(root)
    pnuma_nodes = parse_nodeset(pnuma_str)
    num_vnodes = len(pnuma_nodes)

    for tag in ["cputune", "numatune"]:
        el = root.find(f"./{tag}")
        if el is not None:
            root.remove(el)

    cpu = root.find("./cpu")
    if cpu is None:
        cpu = ET.SubElement(root, "cpu", mode="host-passthrough")
        indent_node(cpu)
    else:
        numa_el = cpu.find("./numa")
        if numa_el is not None:
            cpu.remove(numa_el)

    topo_el = cpu.find("./topology")
    if topo_el is not None:
        cpu.remove(topo_el)

    if not cpu.text or not cpu.text.strip():
        cpu.text = "\n    "

    # Inject a CPU topology that is coherent with the vNUMA one
    if vcpus % num_vnodes != 0:
        print(f"Warning: Asymmetric topology. {vcpus} vCPUs not divisible by {num_vnodes} NUMA nodes.", file=sys.stderr)
        # Fallback to a flat 1 vcpu = 1 socket. Makes QEMU happy but might crash the guest... :-(
        sockets, cores, threads = vcpus, 1, 1
    else:
        sockets, cores, threads = num_vnodes, vcpus // num_vnodes, 1

    topo = ET.SubElement(cpu, "topology", sockets=str(sockets), cores=str(cores), threads=str(threads))
    indent_node(topo, level=2)
    topo.tail = "\n    "

    vnuma = ET.SubElement(cpu, "numa")
    cputune = ET.Element("cputune")
    numatune = ET.Element("numatune")
    ET.SubElement(numatune, "memory", mode="strict", nodeset=pnuma_str)

    vcpus_per_node = vcpus // num_vnodes
    mem_per_node = mem_kib // num_vnodes

    for i, pnode in enumerate(pnuma_nodes):
        start_vcpu = i * vcpus_per_node
        end_vcpu = start_vcpu + vcpus_per_node - 1
        if i == num_vnodes - 1:
            end_vcpu = vcpus - 1

        cpus_str = f"{start_vcpu}-{end_vcpu}"
        pcpu_mask = sysfs_mapper_func(pnode)

        ET.SubElement(vnuma, "cell", id=str(i), cpus=cpus_str, memory=str(mem_per_node), unit="KiB")
        ET.SubElement(numatune, "memnode", cellid=str(i), mode="strict", nodeset=str(pnode))

        for v in range(start_vcpu, end_vcpu + 1):
            ET.SubElement(cputune, "vcpupin", vcpu=str(v), cpuset=pcpu_mask)

    indent_node(vnuma, level=2)
    vnuma.tail = "\n  "
    indent_node(numatune, level=1)
    indent_node(cputune, level=1)

    insert_idx = 0
    for idx, child in enumerate(root):
        tag_name = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag_name in ["memory", "currentMemory"]:
            insert_idx = idx + 1
            
    root.insert(insert_idx, numatune)
    root.insert(insert_idx + 1, cputune)
    numatune.tail = "\n  "
    cputune.tail = "\n  "

def serialize_libvirt_xml(root):
    """
    Serializes the modified lxml ElementTree into a raw string, strictly
    enforcing libvirt's single-quote attribute formatting and stripping
    standard XML declarations.
    """
    raw_str = ET.tostring(root, encoding="utf-8", xml_declaration=False).decode("utf-8")
    def quote_match(m):
        attr, val = m.group(1), m.group(2)
        if attr.startswith("xmlns") or "http" in val:
            return f'{attr}="{val}"'
        return f"{attr}='{val}'"
    return re.sub(r'([\w:]+)="([^"]*)"', quote_match, raw_str).rstrip() + "\n"

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} [define] <domain.xml>", file=sys.stderr)
        sys.exit(1)

    mode = "stdout"
    xml_file = sys.argv[2] if sys.argv[1] == "define" else sys.argv[1]
    mode = "define" if sys.argv[1] == "define" else "stdout"

    parser = ET.XMLParser(remove_blank_text=False)
    tree = ET.parse(xml_file, parser)
    root = tree.getroot()

    vcpus, mem_kib, hp_size = extract_vm_params(root)
    
    cmd = ["numa-preplace", "-w", f"{vcpus}:{mem_kib // 1024}"]
    if hp_size > 0: cmd.extend(["-H", str(hp_size)])
    
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    pnuma_str = res.stdout.strip().split('\n')[-1].strip()

    inject_topology(root, pnuma_str, get_sysfs_cpulist)
    final_xml = serialize_libvirt_xml(root)

    if mode == "define":
        fd, temp_path = tempfile.mkstemp(suffix=".xml", prefix="vnuma_")
        with os.fdopen(fd, 'wb') as f:
            f.write(final_xml.encode("utf-8"))
        subprocess.run(["virsh", "define", temp_path], check=True)
        os.remove(temp_path)
    else:
        sys.stdout.buffer.write(final_xml.encode("utf-8"))

if __name__ == "__main__":
    main()
