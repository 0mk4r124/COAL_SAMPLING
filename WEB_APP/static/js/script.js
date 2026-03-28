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
let emergencyModalOpen = false;
let autoManualModalOpen = false;
let statusCheckInterval = null;

const img = document.getElementById("modalImage");
const wrapper = document.getElementById("zoomWrapper");
const modalElement = document.getElementById('vehicleDetailsModal');
const vehicleModal = new bootstrap.Modal(modalElement);

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

function updateCurrentStatus() {
    "use strict";
    fetch('/api/get_current_status/')
        .then(response => response.json())
        .then(data => {
            // Update state display
            document.getElementById('currentState').textContent = data.current_state || 'IDLE';
            document.getElementById('dashVendorName').textContent = data.vendor_name || 'NOT FOUND';
            document.getElementById('dashVehicleNumber').textContent = data.vehicle_number || 'NOT FOUND';
            
            currentUID = data.uid;
            
            // Handle emergency status
            if (data.emergency === 'active' && !emergencyModalOpen) {
                emergencyModalOpen = true;
                autoManualModalOpen = false;
                showEmergencyModal();
            } else if (data.emergency === null && emergencyModalOpen) {
                // Emergency was just cleared
                emergencyModalOpen = false;
                if (data.emergency_acknowledged) {
                    showEmergencyClearedModal();
                }
            }
            
            // Handle auto_manual status
            if (data.auto_manual === 'manual' && !autoManualModalOpen) {
                autoManualModalOpen = true;
                emergencyModalOpen = false;
                showAutoManualModal();
            }
        })
        .catch(err => console.error("Status update failed:", err));
}

function showEmergencyModal() {
    const emergencyModal = new bootstrap.Modal(document.getElementById('emergencyModal'));
    emergencyModal.show();
}

function showEmergencyClearedModal() {
    const emergencyClearedModal = new bootstrap.Modal(document.getElementById('emergencyClearedModal'));
    emergencyClearedModal.show();
}

function showAutoManualModal() {
    const autoManualModal = new bootstrap.Modal(document.getElementById('autoManualModal'));
    autoManualModal.show();
}

function handleEmergencyRetake() {
    // User wants to retake cycles after emergency
    fetch('/api/acknowledge_emergency/', {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrftoken,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            uid: currentUID,
            retake_cycles: true
        })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            bootstrap.Modal.getInstance(document.getElementById('emergencyClearedModal')).hide();
            alert('Cycles will be retaken. System resuming...');
        }
    })
    .catch(err => console.error(err));
}

function handleEmergencySkip() {
    // User wants to skip cycles after emergency
    fetch('/api/acknowledge_emergency/', {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrftoken,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            uid: currentUID,
            retake_cycles: false
        })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            bootstrap.Modal.getInstance(document.getElementById('emergencyClearedModal')).hide();
            alert('Vehicle marked as completed without additional cycles. System resuming...');
        }
    })
    .catch(err => console.error(err));
}

function handleAutoManualContinue() {
    // User wants to continue with automatic cycles
    fetch('/api/acknowledge_auto_manual/', {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrftoken,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            uid: currentUID,
            user_action: 'continue'
        })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            bootstrap.Modal.getInstance(document.getElementById('autoManualModal')).hide();
            alert('Continuing with automatic cycles...');
        }
    })
    .catch(err => console.error(err));
}

function handleAutoManualSkip() {
    // User wants to skip all cycles
    fetch('/api/acknowledge_auto_manual/', {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrftoken,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            uid: currentUID,
            user_action: 'skip_all_cycles'
        })
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            bootstrap.Modal.getInstance(document.getElementById('autoManualModal')).hide();
            alert('Skipping remaining cycles. Vehicle will be marked as completed...');
        }
    })
    .catch(err => console.error(err));
}

function resetSystemConfirm() {
    const resetModal = new bootstrap.Modal(document.getElementById('resetConfirmModal'));
    resetModal.show();
}

function confirmResetSystem() {
    if (!currentUID) {
        alert('No active vehicle. Cannot reset.');
        return;
    }
    
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
            bootstrap.Modal.getInstance(document.getElementById('resetConfirmModal')).hide();
            alert('System has been reset. All processes marked as ERROR. Ready for next vehicle.');
            // Refresh status
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
            document.getElementById("total_bunch_count").textContent = data.total;
        })
        .catch(err => console.error(err));
}

function populateTable(data, table_name) {
    const tbody = document.getElementById(table_name);
    tbody.innerHTML = '';

    if (table_name === 'History_body'){
        data.forEach(row => {
            const tr = document.createElement('tr');
            const Img1Button = row.img_1_path
                ? `<button class="btn btn-sm btn-primary view-image-btn" data-timestamp="${row.create_time}" data-vehicleNumber="${row.vehicle_number}" data-src="${row.img_1_path}">Image View</button>`
                : "";
            const Img2Button = row.img_2_path
                ? `<button class="btn btn-sm btn-primary view-image-btn" data-timestamp="${row.create_time}" data-vehicleNumber="${row.vehicle_number}" data-src="${row.img_2_path}">Image View</button>`
                : "";
            const Img3Button = row.img_3_path
                ? `<button class="btn btn-sm btn-primary view-image-btn" data-timestamp="${row.create_time}" data-vehicleNumber="${row.vehicle_number}" data-src="${row.img_3_path}">Image View</button>`
                : "";
            const ReportButton = row.report_path
                ? `<button class="btn btn-sm btn-primary view-image-btn" data-timestamp="${row.create_time}" data-vehicleNumber="${row.vehicle_number}" data-src="${row.report_path}">Report View</button>`
                : "";

            tr.innerHTML = `
                <td>${row.sno}</td>
                <td>${row.datetimestamp}</td>
                <td>${row.vehicle_number}</td>
                <td>${row.vendoe_name}</td>
                <td>${row.vendor_code}</td>
                <td>${Img1Button}</td>
                <td>${Img2Button}</td>
                <td>${Img3Button}</td>
                <td>${ReportButton}</td>
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
            if (tab === 'history') {if (!isNaN(page)) fetchHistoryData(page);}
        });
    });
}

// POST REQUEST FOR ADDING VEHICLE AND VENDOR
document.getElementById('submitVehicleDetailsBtn').addEventListener("click", function () {
    const rfid = document.getElementById("rfidInput").value;
    const vehicleNumber = document.getElementById("vehicleNumberInput").value;
    const vendorName = document.getElementById("vendorNameInput").value;
    const vendorCode = document.getElementById("vendorCodeInput").value;
    const bucketNo = document.getElementById("bucketNoInput").value;

    const payload = {
        rfid: rfid,
        vehicleNumber: vehicleNumber,
        vendorName: vendorName,
        vendorCode: vendorCode,
        bucketNo: bucketNo,
    };

    fetch(`/api/add_vehicle/`, {
        method: "POST",
        headers: { 
            "X-CSRFToken": csrftoken,
            "Content-Type": "application/json", },
        body: JSON.stringify(payload),
    })
    .then(r => r.json())
    .then(res => {
        if (res.success) {
            alert("Vehicle updated successfully");

            // Reset form fields
            document.getElementById("vehicleDetailsForm").reset();

            // Close modal
            const modalEl = document.getElementById("vehicleDetailsModal");
            const modalInstance = bootstrap.Modal.getInstance(modalEl);
            modalInstance.hide();

            // Reset modal flag so it can open again for next vehicle
            modalOpen = false;

            // Clear RFID field as well
            document.getElementById("rfidInput").value = "";
        } 
        else {
            alert(res.error || "Failed to update");
        }
    })
    .catch(err => console.error(err));
});

document.addEventListener('click', function (e) {
    const btn = e.target.closest('.view-image-btn');
    if (!btn) return;

    const vehicle_number = btn.dataset.vehicle_number;
    document.getElementById("vehicle_number").innerHTML = `Vehicle Number: <b>${vehicle_number}</b>`;

    const modal = new bootstrap.Modal(document.getElementById('imageModal'));
    modal.show();
    document.getElementById("imageModal").dataset.src = imgSrc;
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

        // Force browser paint before loading
        requestAnimationFrame(() => {
            img.src = "";
            img.src = src;
        });
});

function checkVehicle() {
    fetch('/api/check_for_vehicle/')
        .then(response => response.json())
        .then(data => {

            if (data.status === 'new' && !modalOpen) {
                document.getElementById('rfidInput').value = data.rfid || "";
                vehicleModal.show();
                modalOpen = true;
            }

            if (data.status === 'present') {
                document.getElementById('vendorName').textContent = data.vendor_name;
                document.getElementById('vehicleNumber').textContent = data.vehicle_number;
            }

        })
        .catch(err => console.error("Vehicle check failed:", err));
}

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

// Initialize
document.addEventListener('DOMContentLoaded', function() {
    fetchSystemStatus();
    
    // Update system health status every 10 seconds
    setInterval(fetchSystemStatus, 10000);
    
    // Update current state and emergency/auto_manual status every 2 seconds
    updateCurrentStatus(); // Initial check
    setInterval(updateCurrentStatus, 5000);

    setInterval(checkVehicle, 5000);
    setInterval(updateCameraImages, 5000);
});