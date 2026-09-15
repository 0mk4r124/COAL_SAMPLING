let currentPage = 1;
let scale = 1;
let isDragging = false;
let startX, startY, imgX = 0, imgY = 0;
let currentOpenedAnodeId = null;
let currentOpenedBunch = null;
let isImageLoading = false;
let modalOpen = false;

// State management variables
let currentUID = null;
let vehicleDetailsModalOpen = false;
let emergencyModalOpen = false;
let autoManualModalOpen = false;
let statusCheckInterval = null;

// Print job state
let pendingPrintRow    = null;
let printModalInstance = null;

const img = document.getElementById("modalImage");
const wrapper = document.getElementById("zoomWrapper");
const modalElement = document.getElementById('vehicleDetailsModal');
const vehicleModal = new bootstrap.Modal(modalElement);

let aiModalOpen     = false;
let aiModalInstance = null;
let aiBlockedUID    = null;
let hardModalOpen     = false;
let hardModalInstance = null;
let hardBlockedUID    = null;

function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== "") {
        const cookies = document.cookie.split(";");
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + "=")) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

const csrftoken = getCookie("csrftoken");

// ════════════════════════════════════════════════════════════════════════════
// STATE AND STATUS MANAGEMENT
// ════════════════════════════════════════════════════════════════════════════

let waitModalInstance = null;
let waitModalOpen = false;

function handleBlockingState(data) {
    const modalEl = document.getElementById('waitModal');

    if (data.status === "blocked" && data.reason === "HARD_MATERIAL_BLOCKED") {
        showHardMaterialModal(data);
        return true;
    }
    if (hardModalOpen && data.reason !== "HARD_MATERIAL_BLOCKED") {
        hardModalOpen = false;
        if (hardModalInstance) hardModalInstance.hide();
    }
 
    if (data.status === "blocked" && data.reason === "AI_BLOCKED") {
        showAiBlockedModal(data);
        return true;                      // stop further UI updates
    }
    // If we were showing the AI modal and the manager has moved on
    // (operator answered, or the decision timed out), close it.
    if (aiModalOpen && data.reason !== "AI_BLOCKED") {
        aiModalOpen = false;
        if (aiModalInstance) aiModalInstance.hide();
    }

    if (!waitModalInstance) {
        waitModalInstance = new bootstrap.Modal(modalEl, {
            backdrop: 'static',
            keyboard: false
        });
    }

    if (data.status === "blocked") {
        if (!waitModalOpen) {
            waitModalOpen = true;
            function fetchVehicleMaster() {
                fetch('/api/vehicle_master/')
                    .then(res => res.json())
                    .then(data => {
                        const tbody = document.getElementById('vehicleMasterBody');
                        tbody.innerHTML = '';

                        data.data.forEach(row => {
                            const tr = document.createElement('tr');

                            tr.innerHTML = `
                                <td>${row.rfid}</td>
                                <td>${row.vehicle_number}</td>
                                <td>${row.vendor_name}</td>
                                <td>${row.vendor_code}</td>
                                <td>
                                    <button class="btn btn-sm btn-warning"
                                        onclick='openEditModal(${JSON.stringify(row)})'>
                                        Edit
                                    </button>
                                </td>
                            `;

                            tbody.appendChild(tr);
                        });
                    });
            }
            const message = document.getElementById('waitModalMessage');

            if (data.reason === "EMERGENCY_ACTIVE") {
                message.textContent = "Emergency is ACTIVE. Waiting until it is cleared...";
            } 
            else if (data.reason === "AUTO_MANUAL_ACTIVE") {
                message.textContent = "Manual mode is ACTIVE. Waiting until it is cleared...";
            } 
            else {
                message.textContent = "System is blocked. Waiting...";
            }

            waitModalInstance.show();
        }

        return true; // block further UI updates
    }

    // ✅ HIDE MODAL when cleared
    if (waitModalOpen) {
        waitModalOpen = false;
        waitModalInstance.hide();
    }

    return false;
}

document.getElementById('vendorNameInput').addEventListener('input', function () {

    const inputValue = this.value.trim().toLowerCase();

    const vendorCodeInput = document.getElementById('vendorCodeInput');
    // const bucketInput = document.getElementById('bucketNoInput');

    // Try to find match
    const match = window.vendorCache.find(v => 
        v.vendor_name.toLowerCase() === inputValue
    );

    if (match) {
        // ✅ Existing vendor → autofill
        vendorCodeInput.value = match.vendor_code || '';
        // bucketInput.value = match.bucket_no || '';

        vendorCodeInput.readOnly = true;
        // bucketInput.readOnly = true;
    } else {
        // ✅ New vendor → allow input
        vendorCodeInput.value = '';
        // bucketInput.value = '';

        vendorCodeInput.readOnly = false;
        // bucketInput.readOnly = false;
    }
});

document.getElementById("vehicleNumberInput").addEventListener("input", function() {
    this.value = this.value.toUpperCase();
});

document.getElementById('vehicleDetailsModal').addEventListener('hidden.bs.modal', function () {
    vehicleDetailsModalOpen = false;
});

function updateCurrentStatus() {
    "use strict";
    fetch('/api/get_current_status/')
        .then(response => response.json())
        .then(data => {
            renderStatusPanel(data);
            renderLiveTruck(data);

            // ── Session identity and buttons FIRST ──────────────────────────
            // These have to run on every poll, including blocked ones. They
            // used to sit below the blocking guard, so entering any wait state
            // froze currentUID and lastErrorUID — and Reset System refuses to
            // fire without a currentUID, which is precisely when an operator
            // needs it most.
            currentUID   = data.uid || null;
            lastErrorUID = data.last_error_uid || null;

            // const retakeBtn = document.getElementById('retakeBtn');
            // if (retakeBtn) {
            //     retakeBtn.disabled = !data.retake_available;
            // }

            // const printCurrentBtn = document.getElementById('printCurrentBtn');
            // if (printCurrentBtn) {
            //     const isInProgress = data.status === 'in_progress'
            //         && !!data.vehicle_number
            //         && data.vehicle_number !== 'NOT_FOUND';
            //     printCurrentBtn.disabled = !isInProgress;
            //     window._currentVehicleData = isInProgress ? {
            //         vehicle_number: data.vehicle_number,
            //         vendor_name:    data.vendor_name,
            //         uid:            currentUID,
            //         datetimestamp:  currentUID,
            //     } : null;
            // }

            // ── Blocking states ─────────────────────────────────────────────
            // let blocked = false;
            // try {
            //     blocked = handleBlockingState(data);
            // } catch (e) {
            //     console.error('handleBlockingState failed:', e);
            // }
            // if (blocked) {
            //     return; // stop everything else
            // }

            if (handleBlockingState(data)) {
                return; // stop everything else
            }

            if (data.add_vehicle === "YES" && !vehicleDetailsModalOpen) {

                const input = document.getElementById('rfidInput');
                input.value = data.rfids || '';
                input.disabled = true;

                window.vendorCache = data.vendors || [];
                const datalist = document.getElementById('vendorList');
                datalist.innerHTML = '';
                window.vendorCache.forEach(v => {
                    const opt = document.createElement('option');
                    opt.value = v.vendor_name;
                    datalist.appendChild(opt);
                });
            
                vehicleDetailsModalOpen = true;
                const modal = new bootstrap.Modal(document.getElementById('vehicleDetailsModal'));
                modal.show();
            }

            document.getElementById('currentState').textContent = data.current_state || 'IDLE';
            document.getElementById('dashVendorName').textContent = data.vendor_name || 'NOT FOUND';
            document.getElementById('dashVehicleNumber').textContent = data.vehicle_number || 'NOT FOUND';
            
            currentUID = data.uid;

            // ── Print Current Vehicle button ──────────────────────────────────
            const printCurrentBtn = document.getElementById('printCurrentBtn');
            if (printCurrentBtn) {
                const isInProgress = data.status === 'in_progress' && !!data.vehicle_number && data.vehicle_number !== 'NOT_FOUND';
                printCurrentBtn.disabled = !isInProgress;
                // Cache the data so printCurrentVehicle() can use it immediately
                window._currentVehicleData = isInProgress ? {
                    vehicle_number: data.vehicle_number,
                    vendor_name:    data.vendor_name,
                    uid:  currentUID,
                    datetimestamp:  currentUID,
                } : null;
            }

            // ── Retake button ─────────────────────────────────────────────────
            const retakeBtn = document.getElementById('retakeBtn');
            if (retakeBtn) {
                retakeBtn.disabled = !data.retake_available;
            }
            lastErrorUID = data.last_error_uid || null;

            let blocked = false;
            try {
                blocked = handleBlockingState(data);
            } catch (e) {
                console.error('handleBlockingState failed:', e);
            }
            if (blocked) {
                return; // stop everything else
            }
            
        })
        .catch(err => console.error("Status update failed:", err));
}

function resetSystemConfirm() {
    const resetModal = new bootstrap.Modal(document.getElementById('resetConfirmModal'));
    resetModal.show();
}

function confirmResetSystem() {
    // No uid guard here any more. The server finds whatever is IN_PROGRESS if
    // the browser's uid is stale, and the old client-side guard just meant the
    // button silently refused to work in exactly the states it exists for.
    fetch('/api/reset_system/', {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrftoken,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ uid: currentUID })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            const modal = bootstrap.Modal.getInstance(document.getElementById('resetConfirmModal'));
            if (modal) modal.hide();

            if (res.marked) {
                alert('System reset. Session ' + res.marked_uid +
                      ' marked as ERROR.\nUse "Retake Failed Cycle" to re-run it.');
            } else {
                alert('System reset, but no session was in progress — there is ' +
                      'nothing to retake.');
            }

            // Pull the new retake_available straight away rather than waiting
            // for the next 2 s poll
            updateCurrentStatus();
        } else {
            alert('Error: ' + (res.error || 'Unknown error'));
        }
    })
    .catch(err => {
        console.error(err);
        alert('Error resetting system');
    });
}

// Reset zoom & position whenever modal opens
document.getElementById("imageModal").addEventListener("shown.bs.modal", () => {
    scale = 1;
    imgX = imgY = 0;
    img.style.transform = `translate(0px, 0px) scale(1)`;
});

// Mouse wheel zoom
wrapper.addEventListener("wheel", (e) => {
    e.preventDefault();
    const delta = e.deltaY < 0 ? 0.1 : -0.1;
    scale = Math.min(Math.max(0.5, scale + delta), 4); // limit zoom 0.5x – 4x
    img.style.transform = `translate(${imgX}px, ${imgY}px) scale(${scale})`;
});

// Drag image
wrapper.addEventListener("mousedown", (e) => {
    isDragging = true;
    wrapper.style.cursor = "grabbing";
    startX = e.clientX - imgX;
    startY = e.clientY - imgY;
});

wrapper.addEventListener("mousemove", (e) => {
    if (!isDragging) return;
    imgX = e.clientX - startX;
    imgY = e.clientY - startY;
    img.style.transform = `translate(${imgX}px, ${imgY}px) scale(${scale})`;
});

wrapper.addEventListener("mouseup", () => {
    isDragging = false;
    wrapper.style.cursor = "grab";
});

wrapper.addEventListener("mouseleave", () => {
    isDragging = false;
});

function downloadHistoryData() {
    const dateRange = document.getElementById('date_range').value;

    if (!dateRange) {
        alert("Please select a date range");
        return;
    }

    let startDate, endDate;

    if (dateRange.includes(" - ")) {
        [startDate, endDate] = dateRange.split(" - ").map(s => s.trim());
    } else {
        startDate = endDate = dateRange.trim();
    }

    const vehicleNumber = document.getElementById('vehicle_number_search').value.trim();
    const vendorName = document.getElementById('vendor_name_search').value.trim();

    let url = `/api/download_history_data/?start_date=${startDate}&end_date=${endDate}`;

    if (vehicleNumber) {
        url += `&vehicle_number=${encodeURIComponent(vehicleNumber)}`;
    }

    if (vendorName) {
        url += `&vendor_name=${encodeURIComponent(vendorName)}`;
    }

    window.open(url, '_blank');
}

function sendPrintData(row) {
    pendingPrintRow = row;

    // Reset modal to initial state
    const btn = document.getElementById('confirmPrintBtn');
    btn.disabled  = false;
    btn.innerHTML = '<i class="fas fa-print"></i> Send Print';

    document.getElementById('printConfirmText').innerHTML =
        `Send print job for: <strong>${row.vehicle_number}</strong> &mdash; ${row.vendor_name}<br>
         <small class="text-muted">${row.datetimestamp}</small>`;

    if (!printModalInstance) {
        printModalInstance = new bootstrap.Modal(
            document.getElementById('printConfirmModal'),
            { backdrop: 'static', keyboard: false }
        );
    }
    printModalInstance.show();
}

function confirmSendPrint() {
    if (!pendingPrintRow) return;

    const row       = pendingPrintRow;
    const btn       = document.getElementById('confirmPrintBtn');
    const isCurrent = row._source === 'current';

    btn.disabled  = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Sending...';

    const endpoint = isCurrent ? '/api/print_current_vehicle/' : '/api/send_print_data/';
    const body     = isCurrent
        ? {}
        : { vehicle_number: row.vehicle_number, vendor_name: row.vendor_name, dtstamp: row.uid };

    fetch(endpoint, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrftoken, 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            if (printModalInstance) printModalInstance.hide();
        } else {
            btn.disabled  = false;
            btn.innerHTML = '<i class="fas fa-print"></i> Send Print';
            alert('❌ Print failed: ' + (res.error || 'Unknown error'));
        }
    })
    .catch(err => {
        btn.disabled  = false;
        btn.innerHTML = '<i class="fas fa-print"></i> Send Print';
        console.error(err);
        alert('❌ Network error while sending print job');
    });
}

function printCurrentVehicle() {
    const d = window._currentVehicleData;
    if (!d) {
        alert('No vehicle currently in progress.');
        return;
    }

    // Populate the shared print modal with current vehicle data
    pendingPrintRow = {
        vehicle_number: d.vehicle_number,
        vendor_name:    d.vendor_name,
        datetimestamp:  d.uid,
        uid:  d.uid,
        _source:        'current'   // flag so confirmSendPrint hits the right endpoint
    };

    const btn = document.getElementById('confirmPrintBtn');
    btn.disabled  = false;
    btn.innerHTML = '<i class="fas fa-print"></i> Send Print';

    document.getElementById('printConfirmText').innerHTML =
        `Print current vehicle: <strong>${d.vehicle_number}</strong> &mdash; ${d.vendor_name}<br>
         <small class="text-muted">${d.datetimestamp}</small>`;

    if (!printModalInstance) {
        printModalInstance = new bootstrap.Modal(
            document.getElementById('printConfirmModal'),
            { backdrop: 'static', keyboard: false }
        );
    }
    printModalInstance.show();
}

// ════════════════════════════════════════════════════════════════════════════
// RETAKE FAILED CYCLE
// ════════════════════════════════════════════════════════════════════════════

let lastErrorUID        = null;
let retakeModalInstance = null;

function retakeCycle() {
    if (!lastErrorUID) {
        alert('No failed cycle available to retake.');
        return;
    }
    if (!retakeModalInstance) {
        retakeModalInstance = new bootstrap.Modal(
            document.getElementById('retakeConfirmModal'),
            { backdrop: 'static', keyboard: false }
        );
    }
    retakeModalInstance.show();
}

function confirmRetakeCycle() {
    if (!lastErrorUID) return;

    const btn = document.getElementById('confirmRetakeBtn');
    btn.disabled  = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Starting...';

    fetch('/api/retake_failed_cycle/', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrftoken, 'Content-Type': 'application/json' },
        body: JSON.stringify({ uid: lastErrorUID })
    })
    .then(r => r.json())
    .then(res => {
        btn.disabled  = false;
        btn.innerHTML = '<i class="fas fa-redo"></i> Yes, Retake';
        if (res.success) {
            retakeModalInstance.hide();
            alert('✅ Retake cycle started.\nNew session: ' + res.new_uid);
        } else {
            alert('❌ Retake failed: ' + (res.error || 'Unknown error'));
        }
    })
    .catch(err => {
        btn.disabled  = false;
        btn.innerHTML = '<i class="fas fa-redo"></i> Yes, Retake';
        console.error(err);
        alert('❌ Network error during retake');
    });
}

function fetchVehicleMaster(page=1) {
    fetch(`/api/vehicle_master/?page=${page}`)
        .then(res => res.json())
        .then(data => {
            const tbody = document.getElementById('vehicleMasterBody');
            tbody.innerHTML = '';

            data.data.forEach(row => {
                const tr = document.createElement('tr');

                tr.innerHTML = `
                    <td>${row.sno}</td>
                    <td>${row.rfid}</td>
                    <td>${row.vehicle_number}</td>
                    <td>${row.vendor_name}</td>
                    <td>${row.vendor_code}</td>
                    <td>
                        <button class="btn btn-sm btn-warning"
                            onclick='openEditModal(${JSON.stringify(row)})'>
                            Edit
                        </button>
                    </td>
                `;

                tbody.appendChild(tr);
            });

            setupPagination(
                data.total,
                data.page,
                data.per_page,
                "vehicle_master"
            );
        });
}

document.addEventListener("DOMContentLoaded", function () {
    // Initialize daterangepicker with default last 7 days
    $('#date_range').daterangepicker({
        autoUpdateInput: true,
        startDate: moment().subtract(0, 'days'),
        endDate: moment(),
        locale: {
            format: "YYYY-MM-DD",
            cancelLabel: "Clear",
            applyLabel: "Apply"
        },
        opens: "right",
        drops: "down",
        autoApply: false,
        buttonClasses: ['btn', 'btn-sm', 'btn-primary'],
    }, function (start, end) {
        $('#date_range').val(start.format("YYYY-MM-DD") + " - " + end.format("YYYY-MM-DD"));
    });

    // Apply event
    $('#date_range').on("apply.daterangepicker", function (ev, picker) {
        const start = picker.startDate.format("YYYY-MM-DD");
        const end = picker.endDate.format("YYYY-MM-DD");
        $(this).val(start === end ? start : start + " - " + end);
    });

    // Cancel event
    $('#date_range').on("cancel.daterangepicker", function () {
        $(this).val("");
    });
});

function fetchHistoryData(page=1) {
    const dateRange = document.getElementById('date_range').value;
    if (!dateRange) {
        alert("Please select a date range");
        return;
    }
    if (dateRange.includes(" - ")) {
        [startDate, endDate] = dateRange.split(" - ").map(s => s.trim());
    } else {
        startDate = endDate = dateRange.trim();
    }
    
    const vehicleNumber = document.getElementById('vehicle_number_search').value.trim();
    const vendorNumber = document.getElementById('vendor_name_search').value.trim();
    let url = `/api/fetch_history_data/?start_date=${startDate}&end_date=${endDate}&page=${page}`;
    if (vehicleNumber) {
        url += `&vehicle_number=${encodeURIComponent(vehicleNumber)}`;
    }
    if (vendorNumber) {
        url += `&vendor_name=${encodeURIComponent(vendorNumber)}`;
    }
    
    fetch(url)
        .then(response => response.json())
        .then(data => {
            populateTable(data.data, 'History_body');
            setupPagination(data.total, data.page, data.per_page, "history");
        })
        .catch(err => console.error(err));
}

function populateTable(data, table_name) {
    const tbody = document.getElementById(table_name);
    tbody.innerHTML = '';

    if (table_name === 'History_body'){
        data.forEach(row => {
            const tr = document.createElement('tr');

            const VehicleImgButton = row.vehicle_image
                ? `<button class="btn btn-sm btn-primary view-image-btn" data-timestamp="${row.create_time}" data-vehicle_number="${row.vehicle_number}" data-src="${row.vehicle_image}">Vehicle View</button>`
                : "";

            const ReportButton = row.report_path
                ? `<button class="btn btn-sm btn-primary" onclick="window.open('/api/serve-file/?file=${encodeURIComponent(row.report_path)}', '_blank')">Report</button>`
                : "";

            const PrintButton = `<button class="btn btn-sm btn-warning" onclick="sendPrintData(${JSON.stringify(row).replace(/"/g, '&quot;')})">
                <i class="fas fa-print"></i> Print
            </button>`;

            const SampleMapButton = row.sample_info
                ? `<button class="btn btn-sm btn-dark"
                           onclick='openSampleMap(${JSON.stringify(row).replace(/'/g, "&#39;")})'>
                       <i class="fas fa-map-marker-alt"></i> Samples
                   </button>`
                : `<span class="text-muted small">&mdash;</span>`;

            tr.innerHTML = `
                <td>${row.sno}</td>
                <td class="small">${row.cycle_start || '-'}</td>
                <td class="small">${row.cycle_end || '-'}</td>
                <td class="fw-semibold">${row.cycle_time || '-'}</td>
                <td>${row.vehicle_number}</td>
                <td>${row.vendor_name}</td>
                <td>${row.vendor_code}</td>
                <td>${row.bucket_no}</td>
                <td>${VehicleImgButton}</td>
                <td>${SampleMapButton}</td>
                <td>${ReportButton}</td>
                <td>${PrintButton}</td>
            `;
            tbody.appendChild(tr);
        });
    }
}

function setupPagination(total, current, perPage, tab) {
    const totalPages = Math.ceil(total / perPage);

    const pagination = {
        total_records: total,
        current_page: current,
        has_previous: current > 1,
        has_next: current < totalPages,
        start_index: (current - 1) * perPage + 1,
        end_index: Math.min(current * perPage, total),
        custom_page_range: getCustomPageRange(current, totalPages)
    };

    renderPagination(pagination, tab);
}

function getCustomPageRange(current, totalPages) {
    const delta = 2;
    const range = [];
    for (let i = Math.max(1, current - delta); i <= Math.min(totalPages, current + delta); i++) {
        range.push(i);
    }
    if (range[0] > 1) {
        if (range[0] > 2) range.unshift('...');
        range.unshift(1);
    }
    if (range[range.length - 1] < totalPages) {
        if (range[range.length - 1] < totalPages - 1) range.push('...');
        range.push(totalPages);
    }
    return range;
}

function renderPagination(pagination, tab) {
    const container = document.getElementById(`pagination-${tab}`);
    if (!pagination) return;

    let html = `
        <div>
            <p class="mb-0 text-muted small">
                Showing ${pagination.start_index}–${pagination.end_index} of ${pagination.total_records} entries
            </p>
        </div>

        <div>
            <nav>
                <ul class="pagination mb-0">
                ${pagination.has_previous ?
                    `<li class="page-item"><a class="page-link" href="#" data-page="${pagination.current_page - 1}">Previous</a></li>` :
                    `<li class="page-item disabled"><span class="page-link">Previous</span></li>`}
    `;

    pagination.custom_page_range.forEach(num => {
        if (num === '...') {
            html += `<li class="page-item disabled"><span class="page-link">…</span></li>`;
        } else if (num === pagination.current_page) {
            html += `<li class="page-item active"><span class="page-link">${num}</span></li>`;
        } else {
            html += `<li class="page-item"><a class="page-link" href="#" data-page="${num}">${num}</a></li>`;
        }
    });

    html += `
                ${pagination.has_next ?
                    `<li class="page-item"><a class="page-link" href="#" data-page="${pagination.current_page + 1}">Next</a></li>` :
                    `<li class="page-item disabled"><span class="page-link">Next</span></li>`}
                </ul>
            </nav>
        </div>
    `;

    container.innerHTML = html;

    container.querySelectorAll(".page-link[data-page]").forEach(link => {
        link.addEventListener("click", function (e) {
            e.preventDefault();
            const page = parseInt(this.getAttribute("data-page"));
            if (tab === 'history') {
                if (!isNaN(page)) fetchHistoryData(page);
            }
            if (tab === 'vehicle_master') {
                if (!isNaN(page)) fetchVehicleMaster(page);
            }
            if (tab === 'samplemap') {
                if (!isNaN(page)) sampleMapGoto(page);
            }
        });
    });
}

function downloadVehicleMaster() {
    window.open('/api/download_vehicle_master/', '_blank');
}

function openEditModal(data) {
    document.getElementById('edit_rfid').value = data.rfid;
    document.getElementById('edit_vehicle_number').value = data.vehicle_number;
    document.getElementById('edit_vendor_name').value = data.vendor_name;
    document.getElementById('edit_vendor_code').value = data.vendor_code;

    new bootstrap.Modal(document.getElementById('editVehicleModal')).show();
}

function submitVehicleEdit() {
    const payload = {
        rfid: document.getElementById('edit_rfid').value,
        vehicle_number: document.getElementById('edit_vehicle_number').value,
        vendor_name: document.getElementById('edit_vendor_name').value,
        vendor_code: document.getElementById('edit_vendor_code').value,
    };

    fetch('/api/edit_vehicle_master/', {
        method: 'POST',
        headers: {
            "X-CSRFToken": csrftoken,
            "Content-Type": "application/json"
        },
        body: JSON.stringify(payload)
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            alert("Updated successfully");
            fetchVehicleMaster();
            bootstrap.Modal.getInstance(document.getElementById('editVehicleModal')).hide();
        } else {
            alert(res.error);
        }
    });
}

function uploadVehicleMaster() {
    const data = prompt("Paste JSON data");

    if (!data) return;

    fetch('/api/upload_vehicle_master/', {
        method: 'POST',
        headers: {
            "X-CSRFToken": csrftoken,
            "Content-Type": "application/json"
        },
        body: data
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            alert("Uploaded successfully");
            fetchVehicleMaster();
        } else {
            alert(res.error);
        }
    });
}

// POST REQUEST FOR ADDING VEHICLE AND VENDOR
document.getElementById('submitVehicleDetailsBtn').addEventListener("click", function () {

    const rfid = document.getElementById("rfidInput").value;
    const vehicleNumber = document.getElementById("vehicleNumberInput").value;

    const vendorNameInput = document.getElementById("vendorNameInput").value;
    const vendorCodeInput = document.getElementById("vendorCodeInput").value;

    let payload = {
        rfid: rfid,
        vehicleNumber: vehicleNumber,
        vendorName: vendorNameInput,
        vendorCode: vendorCodeInput,
        // bucketNo: bucketNo,
    };

    fetch(`/api/add_vehicle/`, {
        method: "POST",
        headers: { 
            "X-CSRFToken": csrftoken,
            "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            alert("Vehicle updated successfully");

            document.getElementById("vehicleDetailsForm").reset();

            const modalEl = document.getElementById("vehicleDetailsModal");
            bootstrap.Modal.getInstance(modalEl).hide();

            vehicleDetailsModalOpen = false;
        } else {
            alert(res.error || "Failed to update");
        }
    })
    .catch(err => console.error(err));
});

document.addEventListener('click', function (e) {
    const btn = e.target.closest('.view-image-btn');
    if (!btn) return;

    const vehicle_number = btn.dataset.vehicle_number;
    const imgSrc = btn.dataset.src;
    document.getElementById("vehicle_number").innerHTML = `Vehicle Number: <b>${vehicle_number}</b>`;

    const modalEl = document.getElementById('imageModal');
    modalEl.dataset.src = imgSrc;

    const modal = new bootstrap.Modal(modalEl);
    modal.show();
});

document.getElementById("imageModal")
    .addEventListener("hidden.bs.modal", function () {
        const img = document.getElementById("modalImage");
        const loader = document.getElementById("imageLoader");

        isImageLoading = false;
        img.src = "";
        img.style.display = "none";
        loader.style.display = "none";
});

document.getElementById("imageModal")
.addEventListener("shown.bs.modal", function () {

    const img = document.getElementById("modalImage");
    const loader = document.getElementById("imageLoader");
    const src = this.dataset.src;

    if (!src) return;

    isImageLoading = true;

    loader.style.display = "block";
    img.style.display = "none";

    img.onload = function () {
        loader.style.display = "none";
        img.style.display = "block";
        isImageLoading = false;
    };

    img.onerror = function () {
        loader.style.display = "none";
        img.style.display = "none";
        isImageLoading = false;
    };

    img.src = src;
});

function updateSystemStatus() {
    const cameraContainer = document.getElementById('camera-indicators');
    const locationContainer = document.getElementById('location-dots');
    
    cameraContainer.innerHTML = '';
    locationContainer.innerHTML = '';

    // Create invisible location info dots
    Object.entries(systemData).forEach(([key, info]) => {
        const dot = document.createElement('div');
        let statusClass = '';

        if (info.type && info.type == 'info') {
            dot.className = 'fs-5 location-dot';
        }
        else {
            if (info.status) statusClass = info.status.toLowerCase();
            dot.className = `cam-dot ${statusClass}`;
        }
        
        dot.style.top = `${info.top}%`;
        dot.style.left = `${info.left}%`;

        let tooltipText = `${key}\n`;

        if (info.name) tooltipText += `Name: ${info.name}\n`;

        if (info.current_stem) tooltipText += `Current Stem: ${info.current_stem}\n`;
        if (info.current_bunch) tooltipText += `Current Bunch: ${info.current_bunch}\n`;
        if (info.anodes) tooltipText += `Current Anodes: ${info.anodes}\n`;

        if (info.status) tooltipText += `Status: ${info.status}\n`;
        if (info.ip) tooltipText += `IP: ${info.ip}\n`;
        if (info.cam_serial) tooltipText += `Cam Serial: ${info.cam_serial}\n`;

        if (info.last_ping) tooltipText += `Last Ping: ${info.last_ping}\n`;

        dot.dataset.tooltip = tooltipText.trim();
        locationContainer.appendChild(dot);
    });
}

// Fetch from API (commented out for testing)
async function fetchSystemStatus() {
    try {
        // Uncomment when API is ready
        const response = await fetch('/api/health_status/');
        const data = await response.json();
        systemData = data.data;
        
        updateSystemStatus();
    } catch (error) {
        console.error('Error fetching system status:', error);
    }
}

function updateCameraImages() {
    const url = `/api/live-ip-camera`;
    fetch(url)
        .then(res => res.json())
        .then(data => {
            if (data.status === "success") {
                data.cameras.forEach((cam, idx) => {
                    const slot = document.getElementById(`camera-slot-${idx + 1}`);
                    if (!slot) return;
                    let img = slot.querySelector("img");

                    if (cam.image_url) {
                        if (!img) {
                            // Create img once if it doesn't exist
                            img = document.createElement("img");
                            slot.innerHTML = "";  // clear slot only first time
                            slot.appendChild(img);
                        }
                        // Update src without creating new element → no blink
                        img.src = `${cam.image_url}?t=${Date.now()}`;
                    } else {
                        slot.innerHTML = "No Camera Selected";
                    }
                });
            }
        })
        .catch(err => console.error("Error fetching camera images:", err));
}

// Update on user activity
function resetActivityTimer() {
    lastActivityTime = Date.now();
}

// Listen to common user actions
['click', 'mousemove', 'keydown', 'scroll', 'touchstart'].forEach(event => {
    document.addEventListener(event, resetActivityTimer, true);
});

// Initialize
document.addEventListener('DOMContentLoaded', function() {

    // First run
    fetchSystemStatus();
    updateCurrentStatus();
    updateCameraImages();

    // Polling loop
    setInterval(fetchSystemStatus, 5000);
    setInterval(updateCurrentStatus, 2000);
    setInterval(updateCameraImages, 2000);

    // Smart refresh
    setInterval(() => {
        const now = Date.now();
        const inactivityDuration = now - lastActivityTime;
    
        const REFRESH_TIMEOUT = 30 * 60 * 1000;
    
        if (inactivityDuration > REFRESH_TIMEOUT) {
            console.log("Auto-refreshing due to inactivity");
            window.location.reload();
        }
    }, 10 * 1000);

});

// ════════════════════════════════════════════════════════════════════════════
// HARD MATERIAL — operator acknowledgement
// ════════════════════════════════════════════════════════════════════════════

function showHardMaterialModal(data) {
    hardBlockedUID = data.uid;

    const label = document.getElementById('hardBlockedVehicle');
    if (label) {
        label.textContent = `${data.vehicle_number || '-'} — ${data.vendor_name || '-'}`;
    }

    if (hardModalOpen) return;            // already showing; don't re-open every poll

    if (!hardModalInstance) {
        hardModalInstance = new bootstrap.Modal(
            document.getElementById('hardMaterialModal'),
            { backdrop: 'static', keyboard: false }
        );
    }

    const btn = document.getElementById('hardManualBtn');
    if (btn) {
        btn.disabled  = false;
        btn.innerHTML = '<i class="fas fa-hand-paper"></i> Collected Manually — Mark Completed';
    }

    hardModalOpen = true;
    hardModalInstance.show();
}

function handleHardMaterialDecision() {
    if (!hardBlockedUID) {
        alert('No active session to answer.');
        return;
    }

    // Lock immediately — a double click publishes the choice twice and the
    // second message gets consumed by the NEXT block, same failure mode as
    // the AI popup.
    const btn = document.getElementById('hardManualBtn');
    if (btn) {
        btn.disabled  = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Sending...';
    }

    // Same DB channel as the AI decision: the manager reads PLC_COMM.AI_DECISION
    // in HARD_BLOCKED_WAIT and treats MANUAL as "operator collected it by hand,
    // release the vehicle and mark COMPLETED".
    fetch('/api/ai_position_decision/', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrftoken, 'Content-Type': 'application/json' },
        body: JSON.stringify({ uid: hardBlockedUID, decision: 'manual' })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            hardModalOpen = false;
            if (hardModalInstance) hardModalInstance.hide();
        } else {
            if (btn) {
                btn.disabled  = false;
                btn.innerHTML = '<i class="fas fa-hand-paper"></i> Collected Manually — Mark Completed';
            }
            alert('❌ Could not send decision: ' + (res.error || 'Unknown error'));
        }
    })
    .catch(err => {
        if (btn) {
            btn.disabled  = false;
            btn.innerHTML = '<i class="fas fa-hand-paper"></i> Collected Manually — Mark Completed';
        }
        console.error(err);
        alert('❌ Network error while sending the decision');
    });
}
 
function showAiBlockedModal(data) {
    aiBlockedUID = data.uid;
 
    const label = document.getElementById('aiBlockedVehicle');
    if (label) {
        label.textContent = `${data.vehicle_number || '-'} — ${data.vendor_name || '-'}`;
    }
 
    if (aiModalOpen) return;              // already showing; don't re-open on every poll
 
    if (!aiModalInstance) {
        aiModalInstance = new bootstrap.Modal(
            document.getElementById('aiBlockedModal'),
            { backdrop: 'static', keyboard: false }
        );
    }
 
    // Re-enable the buttons for this new prompt
    ['aiContinueBtn', 'aiManualBtn'].forEach(id => {
        const b = document.getElementById(id);
        if (b) b.disabled = false;
    });
 
    aiModalOpen = true;
    aiModalInstance.show();
}
 
function handleAiDecision(decision) {
    if (!aiBlockedUID) {
        alert('No active session to answer.');
        return;
    }
 
    // Lock both buttons immediately — a double click would publish the choice
    // twice and the second message would be consumed by the NEXT block.
    ['aiContinueBtn', 'aiManualBtn'].forEach(id => {
        const b = document.getElementById(id);
        if (b) b.disabled = true;
    });
 
    fetch('/api/ai_position_decision/', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrftoken, 'Content-Type': 'application/json' },
        body: JSON.stringify({ uid: aiBlockedUID, decision: decision })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            aiModalOpen = false;
            if (aiModalInstance) aiModalInstance.hide();
        } else {
            ['aiContinueBtn', 'aiManualBtn'].forEach(id => {
                const b = document.getElementById(id);
                if (b) b.disabled = false;
            });
            alert('❌ Could not send decision: ' + (res.error || 'Unknown error'));
        }
    })
    .catch(err => {
        ['aiContinueBtn', 'aiManualBtn'].forEach(id => {
            const b = document.getElementById(id);
            if (b) b.disabled = false;
        });
        console.error(err);
        alert('❌ Network error while sending the decision');
    });
}

// ════════════════════════════════════════════════════════════════════════════
// SAMPLE MAP — plot sample positions on the truck body
// ════════════════════════════════════════════════════════════════════════════

let sampleMapModalInstance = null;
let sampleMapState = {
    attempts: [], bounds: {}, page: 1, perPage: 6, info: null, row: null
};

// The grey nose of the trailer takes the first 20% of the body, so dots start
// clear of it. The remaining pads keep a 22px dot fully inside the border.
/**
 * Dot placement.
 *
 * X and Y are percentages of FULL auger travel, and the diagram represents the
 * full truck, so a point at X=43 belongs at 43% along the body — full stop.
 *
 * The previous version stretched the reachable band (X 35..100) across the
 * drawn area, which put the dots on a different scale from the grey shallow
 * strip (drawn at 0..20% of the body). One picture, two coordinate systems:
 * a point at X=43 landed at 33%, and the gaps between areas 1/2/3 came out
 * wider than they really are. Everything is on the absolute scale now, so the
 * 20% shallow band and the dots finally agree — and because the auger never
 * travels below X=35, dots still naturally clear the grey strip.
 *
 * The pad keeps a 22px dot from hanging over the border at the extremes.
 */
const TRUCK_INSET = { x1: 24, x2: 95, y1: 14, y2: 86 };
const TRUCK_PAD = { x1: 2, x2: 98, y1: 8, y2: 92 };

function truckClamp(v) {
    const n = Number(v);
    if (v === null || v === undefined || isNaN(n)) return 50;
    return Math.max(0, Math.min(100, n));
}

function truckLeftPct(x) {
    return TRUCK_PAD.x1 + truckClamp(x) * (TRUCK_PAD.x2 - TRUCK_PAD.x1) / 100;
}

function truckTopPct(y) {
    return TRUCK_PAD.y1 + truckClamp(y) * (TRUCK_PAD.y2 - TRUCK_PAD.y1) / 100;
}

// Kept for the bounds shown in tooltips / future use — no longer positions dots.
function truckBounds(info) {
    const b = (info && info.bounds) || {};
    return {
        xMin: b.x_min !== undefined ? b.x_min : 35,
        xMax: b.x_max !== undefined ? b.x_max : 100,
        yMin: b.y_min !== undefined ? b.y_min : 40,
        yMax: b.y_max !== undefined ? b.y_max : 80
    };
}

/**
 * Map an auger travel percentage onto the diagram.
 *
 * The auger only travels X_MIN..X_MAX (35..100) and Y_MIN..Y_MAX (40..80), so
 * the reachable band is stretched across the drawn area instead of being
 * plotted as a raw percentage — that is why dots used to bunch into the middle
 * third of the body and read as misaligned.
 */
function truckPct(v, lo, hi) {
    if (v === null || v === undefined || hi === lo) return 50;
    return Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100));
}

function truckMap(v, lo, hi, a, b) {
    return a + truckPct(v, lo, hi) * (b - a) / 100;
}

function truckBounds(info) {
    const b = (info && info.bounds) || {};
    return {
        xMin: b.x_min !== undefined ? b.x_min : 35,
        xMax: b.x_max !== undefined ? b.x_max : 100,
        yMin: b.y_min !== undefined ? b.y_min : 40,
        yMax: b.y_max !== undefined ? b.y_max : 80
    };
}

function sampleDotClass(a) {
    if (a.status === 'SUCCESS')        return 'sample-dot-ok';
    if (a.reason === 'HARD_MATERIAL')  return 'sample-dot-hard';
    return 'sample-dot-fail';
}

/**
 * Which picture belongs to a dot.
 *
 * Failures carry their own frozen frame (saved by the manager). Successes fall
 * back to SAMPLE_n_IMG_PATH, counted by success order rather than cycle number
 * so a retried cycle still lines up with the right file.
 */
function sampleAttemptImageUrl(a, row) {
    if (a.image) {
        return '/api/serve-file?file=' + encodeURIComponent(a.image);
    }
    if (a.status === 'SUCCESS' && row) {
        const n = sampleMapState.attempts
            .filter(x => x.status === 'SUCCESS' && x.seq <= a.seq).length;
        return row['sample_' + n + '_image'] || null;
    }
    return null;
}

function openSampleAttemptImage(seq) {
    const a = sampleMapState.attempts.find(x => x.seq === seq);
    if (!a) return;

    const src = sampleAttemptImageUrl(a, sampleMapState.row);
    if (!src) {
        alert('No image was saved for attempt #' + seq + '.');
        return;
    }

    const label = document.getElementById('vehicle_number');
    if (label) {
        label.innerHTML =
            `Vehicle Number: <b>${(sampleMapState.row || {}).vehicle_number || '-'}</b>` +
            ` &mdash; attempt #${a.seq} (${a.status}${a.reason ? ' · ' + a.reason.replaceAll('_', ' ') : ''})`;
    }

    const modalEl = document.getElementById('imageModal');
    modalEl.dataset.src = src;
    new bootstrap.Modal(modalEl).show();
}

/**
 * How many attempts fit on one page without the modal scrolling.
 * Clamped to 3..8 so a short window still shows something useful and a tall one
 * doesn't turn the page into a wall of rows.
 */
function sampleMapPerPage() {
    const vh      = window.innerHeight;
    const diagram = Math.min(0.28 * vh, 220);
    const chrome  = 56 + 64 + 34 + 36 + 40 + 34 + 30;  // header, footer, legend,
                                                       // badges, pager, thead, padding
    const rowH    = 34;
    return Math.max(3, Math.min(8, Math.floor((vh * 0.94 - chrome - diagram) / rowH)));
}

function openSampleMap(rowJson) {
    let row;
    try { row = typeof rowJson === 'string' ? JSON.parse(rowJson) : rowJson; }
    catch (e) { console.error(e); return; }

    const info = row.sample_info;

    document.getElementById('sampleMapTitle').textContent =
        `${row.vehicle_number || '-'} — ${row.vendor_name || '-'}`;

    const body   = document.getElementById('sampleMapBody');
    const badges = document.getElementById('sampleMapBadges');
    const wrap   = document.getElementById('sampleMapTableWrap');
    const pager  = document.getElementById('pagination-samplemap');
    const empty  = document.getElementById('sampleMapEmpty');

    body.querySelectorAll('.sample-dot').forEach(d => d.remove());
    document.getElementById('sampleMapRows').innerHTML = '';

    if (!info || !Array.isArray(info.attempts) || info.attempts.length === 0) {
        badges.innerHTML = '';
        wrap.classList.add('d-none');
        pager.classList.add('d-none');
        empty.innerHTML =
            `<div class="alert alert-secondary mb-0 mt-3 text-center">
                No sample position data recorded for this vehicle.
             </div>`;
        showSampleMapModal();
        return;
    }

    empty.innerHTML = '';
    wrap.classList.remove('d-none');
    pager.classList.remove('d-none');

    sampleMapState = {
        attempts: info.attempts,
        bounds:   truckBounds(info),
        page:     1,
        perPage:  sampleMapPerPage(),
        info:     info,
        row:      row
    };

    renderSampleMapPage();
    showSampleMapModal();
}

function sampleMapGoto(page) {
    sampleMapState.page = page;
    renderSampleMapPage();
}

function renderSampleMapPage() {
    const { attempts, bounds, page, perPage, info, row } = sampleMapState;

    const body   = document.getElementById('sampleMapBody');
    const badges = document.getElementById('sampleMapBadges');
    const tbody  = document.getElementById('sampleMapRows');

    body.querySelectorAll('.sample-dot').forEach(d => d.remove());

    const total = attempts.length;
    const start = (page - 1) * perPage;
    const slice = attempts.slice(start, start + perPage);

    // ── Dots on the truck body ──────────────────────────────────────────────
    slice.forEach(a => {
        const dot = document.createElement('div');
        dot.className = 'sample-dot ' + sampleDotClass(a);
        dot.style.left = truckLeftPct(a.x) + '%';
        dot.style.top  = truckTopPct(a.y) + '%';
        dot.textContent = a.seq;
        dot.title =
            `#${a.seq} · Area ${a.area ?? '-'}\n` +
            `${a.status === 'SUCCESS' ? 'Collected' : 'Failed: ' + (a.reason || 'unknown')}\n` +
            `${a.time || ''}\nClick to view image`;
        dot.onclick = () => openSampleAttemptImage(a.seq);
        body.appendChild(dot);
    });

    // ── Counters + collection mode ──────────────────────────────────────────
    const mode = info.collection_mode === 'MANUAL'
        ? `<span class="badge bg-warning text-dark">MANUAL COLLECTION${
              info.manual_reason ? ' · ' + info.manual_reason.replaceAll('_', ' ') : ''}</span>`
        : `<span class="badge bg-secondary">AUTO</span>`;

    badges.innerHTML = `
        <div>
            <span class="badge bg-success">${info.successful || 0} collected</span>
            <span class="badge bg-danger ms-1">${info.failed || 0} failed</span>
        </div>
        <div>${mode}</div>`;

    // ── Rows ────────────────────────────────────────────────────────────────
    const badgeFor = a => {
        if (a.status === 'SUCCESS') return 'bg-success';
        if (a.reason === 'HARD_MATERIAL') return 'bg-primary';
        return 'bg-danger';
    };

    tbody.innerHTML = slice.map(a => {
        const hasImg = !!sampleAttemptImageUrl(a, row);
        const imgBtn = hasImg
            ? `<button class="btn btn-sm btn-outline-dark sample-img-btn"
                       onclick="openSampleAttemptImage(${a.seq})">
                   <i class="fas fa-image"></i> View
               </button>`
            : `<span class="text-muted small">&mdash;</span>`;
        return `
        <tr>
            <td><span class="sample-seq ${sampleDotClass(a)}">${a.seq}</span></td>
            <td>${a.area ?? '-'}</td>
            <td><span class="badge ${badgeFor(a)}">${a.status}</span></td>
            <td>${a.reason ? a.reason.replaceAll('_', ' ') : '-'}</td>
            <td class="text-muted small">${a.time || '-'}</td>
            <td>${imgBtn}</td>
        </tr>`;
    }).join('');

    // Same pagination template as the History and Vehicle Master tabs
    setupPagination(total, page, perPage, 'samplemap');
}

function showSampleMapModal() {
    if (!sampleMapModalInstance) {
        sampleMapModalInstance = new bootstrap.Modal(document.getElementById('sampleMapModal'));
    }
    sampleMapModalInstance.show();
}

// Re-fit when the window is resized while the modal is open, so rotating a
// tablet or snapping the window doesn't reintroduce a scrollbar.
window.addEventListener('resize', () => {
    const el = document.getElementById('sampleMapModal');
    if (!el || !el.classList.contains('show')) return;
    if (!sampleMapState.attempts.length) return;
    sampleMapState.perPage = sampleMapPerPage();
    sampleMapState.page = 1;
    renderSampleMapPage();
});


// ════════════════════════════════════════════════════════════════════════════
// DASHBOARD — status panel + live truck map
// ════════════════════════════════════════════════════════════════════════════

/**
 * What each state actually means, in plant language.
 *
 * `tone` drives the coloured left edge: idle / info / ok / warn / danger.
 */
const STATE_INFO = {
    IDLE: {
        tone: 'idle', head: 'Waiting for a vehicle',
        desc: 'The RFID reader is armed. Nothing is being sampled right now.'
    },
    DB_CHECK: {
        tone: 'info', head: 'Identifying vehicle',
        desc: 'RFID tags read. Looking the vehicle up in the vehicle master.'
    },
    WAITING_FOR_DB: {
        tone: 'warn', head: 'Vehicle not registered',
        desc: 'This RFID is not in the vehicle master. Add the vehicle to let the cycle continue.'
    },
    WAITING_FOR_VEHICLE_MASTER: {
        tone: 'warn', head: 'Vehicle not registered',
        desc: 'This RFID is not in the vehicle master. Add the vehicle to let the cycle continue.'
    },
    OPEN_BARRIER: {
        tone: 'info', head: 'Opening barrier',
        desc: 'Green signal sent to the barrier PLC.'
    },
    BARRIER_OPENING: {
        tone: 'info', head: 'Opening barrier',
        desc: 'Waiting for the barrier to confirm it is fully open.'
    },
    SET_BUCKET: {
        tone: 'info', head: 'Setting bucket',
        desc: 'Writing the bucket number to the PLC so the sample lands in the right bin.'
    },
    VEHICLE_PLACEMENT: {
        tone: 'info', head: 'Positioning vehicle',
        desc: 'Waiting for the truck to settle on the turntable. Sample points are being planned across the body.'
    },
    CLOSE_BARRIER: {
        tone: 'info', head: 'Closing barrier',
        desc: 'Red signal sent — holding the vehicle for sampling.'
    },
    BARRIER_CLOSING: {
        tone: 'info', head: 'Closing barrier',
        desc: 'Waiting for the barrier to confirm it is closed.'
    },
    AUGER_HOME_POS: {
        tone: 'info', head: 'Homing auger',
        desc: 'Auger travelling back to its home position before the next move.'
    },
    CYCLE_POSITION: {
        tone: 'info', head: 'Moving auger to sample point',
        desc: 'X and Y are being driven together to the next sample point on the load.'
    },
    CYCLE_CONFIRM: {
        tone: 'info', head: 'AI checking auger position',
        desc: 'The vision model is confirming the auger is over coal before the cycle is allowed to start.'
    },
    CYCLE_CAPTURE: {
        tone: 'info', head: 'Collecting sample',
        desc: 'Auger is drilling into the load. Waiting for the Z-UP feedback that ends the cycle.'
    },
    CYCLE_DONE: {
        tone: 'info', head: 'Finishing cycle',
        desc: 'Sample taken. Waiting for the cycle-complete confirmation from the PLC.'
    },
    SAMPLE_COLLECTION: {
        tone: 'info', head: 'Discharging sample',
        desc: 'Sample being discharged into the bucket.'
    },
    CYCLE_EMERGENCY_WAIT: {
        tone: 'danger', head: 'EMERGENCY — cycle paused',
        desc: 'Emergency stop is active. The cycle resumes automatically once it is cleared at the panel.'
    },
    AI_BLOCKED_WAIT: {
        tone: 'warn', head: 'Waiting for operator — AI check failed',
        desc: 'The model could not confirm the auger is over coal. Choose continue or manual on the popup.'
    },
    HARD_BLOCKED_WAIT: {
        tone: 'warn', head: 'Waiting for operator — hard material',
        desc: 'Hard material hit twice in a row. Automatic attempts stopped to protect the auger.'
    },
    RED_SIGNAL: {
        tone: 'info', head: 'Holding vehicle',
        desc: 'Red signal held while the last sample is finished.'
    },
    COMPLETE_FINAL: {
        tone: 'ok', head: 'Finishing up',
        desc: 'Generating the report, sending the print slip and preparing to release the vehicle.'
    },
    GREEN_SIGNAL: {
        tone: 'ok', head: 'Releasing vehicle',
        desc: 'Green signal sent. The vehicle may leave the platform.'
    },
    COMPLETE: {
        tone: 'ok', head: 'Cycle complete',
        desc: 'Session closed and logged. Ready for the next vehicle.'
    },
    ERROR: {
        tone: 'danger', head: 'Error — resetting',
        desc: 'The session was aborted. All devices are being reset to a known state.'
    }
};

const TONE_CLASS = {
    idle:   { panel: 'state-idle',   badge: 'bg-secondary', bar: 'bg-secondary' },
    info:   { panel: '',             badge: 'bg-primary',   bar: 'bg-primary' },
    ok:     { panel: 'state-ok',     badge: 'bg-success',   bar: 'bg-success' },
    warn:   { panel: 'state-warn',   badge: 'bg-warning text-dark', bar: 'bg-warning' },
    danger: { panel: 'state-danger', badge: 'bg-danger',    bar: 'bg-danger' }
};

function fmtMMSS(totalSeconds) {
    const s = Math.max(0, parseInt(totalSeconds || 0, 10));
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
}

function renderStatusPanel(data) {
    const panel = document.getElementById('statusPanel');
    if (!panel || !data) return;

    const state = data.current_state || 'IDLE';
    const meta  = STATE_INFO[state] || {
        tone: 'info', head: state.replaceAll('_', ' '),
        desc: 'Cycle in progress.'
    };

    // Emergency / manual override whatever the state says
    let tone = meta.tone;
    let desc = meta.desc;
    if (data.emergency === 'ACTIVE') {
        tone = 'danger';
        desc = 'EMERGENCY STOP is active at the panel. Everything is held until it is cleared.';
    } else if (data.auto_manual === 'ACTIVE') {
        tone = 'warn';
        desc = 'The sampler PLC is in MANUAL. Automatic sampling is paused until it is switched back.';
    }

    const cls = TONE_CLASS[tone] || TONE_CLASS.info;

    panel.classList.remove('state-idle', 'state-ok', 'state-warn', 'state-danger');
    if (cls.panel) panel.classList.add(cls.panel);

    const badge = document.getElementById('currentState');
    badge.className = 'badge fs-6 ' + cls.badge;
    badge.textContent = state;

    document.getElementById('statusHeadline').textContent = meta.head;
    document.getElementById('statusDetail').textContent   = desc;
    document.getElementById('statusBucket').textContent   = data.bucket_number ?? '—';
    document.getElementById('statusUid').textContent      = data.uid || '—';

    // ── Progress ────────────────────────────────────────────────────────────
    const info   = data.sample_info || {};
    const total  = info.total_cycles || 3;
    const done   = info.successful || 0;
    const failed = info.failed || 0;
    const pct    = Math.min(100, Math.round((done / total) * 100));

    const bar = document.getElementById('statusProgress');
    bar.className = 'progress-bar ' + cls.bar;
    bar.style.width = pct + '%';

    const currentCycle = Math.min(total, done + 1);
    const modeTxt = info.collection_mode === 'MANUAL' ? 'MANUAL' : 'AUTO';

    const modeBadge = document.getElementById('statusMode');
    modeBadge.textContent = modeTxt;
    modeBadge.className = 'badge ms-auto ' +
        (modeTxt === 'MANUAL' ? 'bg-warning text-dark' : 'bg-secondary');

    document.getElementById('statusCounters').textContent =
        (data.uid
            ? `Cycle ${currentCycle} of ${total} · ${done} collected · ${failed} failed`
            : `Idle · 0 of ${total}`);

    // Re-anchor the local ticker on every poll. The server value is
    // authoritative; the ticker only fills the 2 s gaps between polls so the
    // clock visibly moves instead of jumping in 2-second steps.
    elapsedBaseSeconds = Number(data.elapsed_seconds) || 0;
    elapsedBaseAt      = Date.now();
    elapsedRunning     = !!data.elapsed_running && !!data.uid;

    document.getElementById('statusElapsed').textContent =
        data.uid ? fmtMMSS(elapsedBaseSeconds) : '00:00';
}

// ════════════════════════════════════════════════════════════════════════════
// ELAPSED CLOCK — smooth the 2 s polling gap
// ════════════════════════════════════════════════════════════════════════════

let elapsedBaseSeconds = 0;   // last value the server reported
let elapsedBaseAt      = 0;   // Date.now() when we received it
let elapsedRunning     = false;

function tickElapsed() {
    if (!elapsedRunning) return;          // finished cycle: frozen at the total
    const el = document.getElementById('statusElapsed');
    if (!el) return;
    const extra = Math.floor((Date.now() - elapsedBaseAt) / 1000);
    el.textContent = fmtMMSS(elapsedBaseSeconds + extra);
}

document.addEventListener('DOMContentLoaded', function () {
    setInterval(tickElapsed, 1000);
});

/**
 * Live sample map on the dashboard.
 *
 * Same geometry as the history modal: auger X runs along the trailer, auger Y
 * across it. The point currently being sampled blinks yellow, then turns green
 * or red the moment the manager records the attempt.
 */
function renderLiveTruck(data) {
    const body = document.getElementById('liveTruckBody');
    if (!body) return;

    body.querySelectorAll('.sample-dot').forEach(d => d.remove());

    const info = (data && data.sample_info) || null;
    if (!info) return;

    const bounds = truckBounds(info);

    const place = (p, cls, label, title) => {
        if (!p) return;
        const dot = document.createElement('div');
        dot.className = 'sample-dot ' + cls;
        dot.style.left = truckLeftPct(p.x) + '%';
        dot.style.top  = truckTopPct(p.y) + '%';
        dot.textContent = label;
        dot.style.cursor = 'default';
        dot.title = title;
        body.appendChild(dot);
    };

    (info.attempts || []).forEach(a => {
        place(a, sampleDotClass(a), a.seq,
            `#${a.seq} · Area ${a.area ?? '-'}\n` +
            `${a.status === 'SUCCESS' ? 'Collected' : 'Failed: ' + (a.reason || 'unknown')}\n` +
            `${a.time || ''}`);
    });

    if (info.current) {
        const seq = (info.attempts || []).length + 1;
        place(info.current, 'sample-dot-live', seq,
            `#${seq} · Area ${info.current.area ?? '-'}\nSampling now…`);
    }
}

// ════════════════════════════════════════════════════════════════════════════
// DASHBOARD — daily stat tiles
// ════════════════════════════════════════════════════════════════════════════

function statPct(part, whole) {
    if (!whole) return '0%';
    return Math.round((part / whole) * 100) + '%';
}

function statDelta(today, yest) {
    const d = (today || 0) - (yest || 0);
    if (d === 0) return 'same as yesterday';
    return (d > 0 ? '+' : '') + d + ' vs yesterday';
}

function renderDashboardStats(s) {
    if (!s || !s.success) return;

    const t = s.today || {};
    const y = s.yesterday || {};
    const w = s.week || {};

    // ── Arrived ─────────────────────────────────────────────────────────────
    document.getElementById('statArrived').textContent = t.arrived ?? 0;
    document.getElementById('statArrivedSub').textContent =
        `yesterday ${y.arrived ?? 0} · ${statDelta(t.arrived, y.arrived)}`;

    document.getElementById('statArrivedTile').setAttribute('data-tooltip',
        `VEHICLES ARRIVED TODAY\n` +
        `Every session opened in VEHICLE_LOGS since midnight.\n\n` +
        `Today: ${t.arrived ?? 0}\n` +
        `  · scanned successfully: ${t.completed ?? 0}\n` +
        `  · failed: ${t.failed ?? 0}\n` +
        `  · in progress: ${t.running ?? 0}\n\n` +
        `Yesterday: ${y.arrived ?? 0}\n` +
        `Last 7 days: ${w.arrived ?? 0} (${w.avg_arrived ?? 0}/day)`
    );

    // ── Scanned ─────────────────────────────────────────────────────────────
    document.getElementById('statScanned').textContent = t.completed ?? 0;
    document.getElementById('statScannedSub').textContent =
        `avg ${fmtMMSS(t.avg_seconds)} · yesterday ${y.completed ?? 0}`;

    document.getElementById('statScannedTile').setAttribute('data-tooltip',
        `SCANNED SUCCESSFULLY\n` +
        `Sessions that reached COMPLETED. Cycle time is measured from the RFID\n` +
        `read to the log being closed.\n\n` +
        `Today: ${t.completed ?? 0} of ${t.arrived ?? 0} (${statPct(t.completed, t.arrived)})\n` +
        `  · average cycle: ${fmtMMSS(t.avg_seconds)}\n` +
        `  · fastest: ${fmtMMSS(t.min_seconds)}   slowest: ${fmtMMSS(t.max_seconds)}\n` +
        `  · timed sessions: ${t.timed ?? 0}\n\n` +
        `Yesterday: ${y.completed ?? 0} · average ${fmtMMSS(y.avg_seconds)}\n` +
        `Last 7 days: ${w.completed ?? 0} (${w.avg_completed ?? 0}/day)`
    );

    // ── Failed ──────────────────────────────────────────────────────────────
    document.getElementById('statFailed').textContent = t.failed ?? 0;
    document.getElementById('statFailedSub').textContent =
        `yesterday ${y.failed ?? 0}` + (t.last_fail ? ` · last ${t.last_fail}` : '');

    document.getElementById('statFailedTile').setAttribute('data-tooltip',
        `FAILED SESSIONS\n` +
        `Sessions marked ERROR — aborted, reset, or timed out mid-cycle.\n` +
        `Use Retake Failed Cycle to re-run the most recent one.\n\n` +
        `Today: ${t.failed ?? 0} of ${t.arrived ?? 0} (${statPct(t.failed, t.arrived)})\n` +
        (t.last_fail ? `  · most recent at ${t.last_fail}\n` : '') +
        `  · still in progress: ${t.running ?? 0}\n\n` +
        `Yesterday: ${y.failed ?? 0} of ${y.arrived ?? 0} (${statPct(y.failed, y.arrived)})`
    );
}

function fetchDashboardStats() {
    fetch('/api/dashboard_stats/')
        .then(r => r.json())
        .then(renderDashboardStats)
        .catch(err => console.error('Dashboard stats update failed:', err));
}

// Separate listener on purpose — leaves the existing DOMContentLoaded block,
// and its camera/status polling intervals, completely untouched.
document.addEventListener('DOMContentLoaded', function () {
    fetchDashboardStats();
    setInterval(fetchDashboardStats, 30000);
});

// ════════════════════════════════════════════════════════════════════════════
// DASHBOARD — fit the shell to whatever space is actually left
// ════════════════════════════════════════════════════════════════════════════

/**
 * Size the dashboard to the real gap between where it starts and where the
 * footer begins, instead of assuming a fixed header height. The CSS
 * calc(100vh - var(--dash-offset)) stays as the pre-JS fallback.
 */
function fitDashboardShell() {
    const shell = document.querySelector('#DASHBOARD .dash-shell');
    if (!shell) return;

    // Tab hidden or not laid out yet — measuring now would give nonsense
    if (shell.offsetParent === null) return;

    const top = shell.getBoundingClientRect().top;
    if (top <= 0) return;

    const footer = document.querySelector('.footer');
    const reserve = (footer ? footer.offsetHeight : 24) + 26;   // footer + card padding

    shell.style.height = Math.max(400, window.innerHeight - top - reserve) + 'px';
}

document.addEventListener('DOMContentLoaded', function () {
    fitDashboardShell();
    setTimeout(fitDashboardShell, 400);   // after webfonts/images settle the layout
    window.addEventListener('resize', fitDashboardShell);

    document.querySelectorAll('[data-bs-target="#DASHBOARD"]').forEach(btn => {
        btn.addEventListener('shown.bs.tab', fitDashboardShell);
    });
});