document.addEventListener("DOMContentLoaded", function () {
  if (typeof DataTable === "undefined") {
    return;
  }

  const nonFilterableTitles = new Set([
    "",
    "Aktion",
    "Aktionen",
    "Bedienung"
  ]);

  document.querySelectorAll(
    "table.datatable"
  ).forEach(function (table) {
    const tableHead = table.tHead;

    if (
      !tableHead
      || tableHead.rows.length === 0
    ) {
      return;
    }

    const titleRow = tableHead.rows[0];
    const titleCells = Array.from(
      titleRow.cells
    );

    const filterRow = document.createElement(
      "tr"
    );

    filterRow.className =
      "datatable-filter-row";

    filterRow.setAttribute(
      "data-dt-order",
      "disable"
    );

    const filterInputs = [];
    const disabledColumnIndexes = [];

    titleCells.forEach(
      function (titleCell, columnIndex) {
        const filterCell =
          document.createElement("th");

        const title =
          titleCell.textContent.trim();

        const filterDisabled = (
          titleCell.dataset.filter === "false"
          || nonFilterableTitles.has(title)
        );

        if (filterDisabled) {
          disabledColumnIndexes.push(
            columnIndex
          );
        } else {
          const input =
            document.createElement("input");

          input.type = "search";
          input.placeholder = title;
          input.setAttribute(
            "aria-label",
            `${title} filtern`
          );

          input.addEventListener(
            "click",
            function (event) {
              event.stopPropagation();
            }
          );

          input.addEventListener(
            "keydown",
            function (event) {
              event.stopPropagation();
            }
          );

          filterCell.appendChild(
            input
          );

          filterInputs.push({
            columnIndex: columnIndex,
            input: input
          });
        }

        filterRow.appendChild(
          filterCell
        );
      }
    );

    tableHead.appendChild(
      filterRow
    );

    const dataTable = new DataTable(
      table,
      {
        titleRow: 0,
        ordering: true,
        searching: true,
        paging: true,
        info: true,
        autoWidth: false,
        pageLength: 25,
        lengthMenu: [
          [10, 25, 50, 100, -1],
          [10, 25, 50, 100, "Alle"]
        ],
        order: [],
        layout: {
          topStart: "pageLength",
          topEnd: "search",
          bottomStart: "info",
          bottomEnd: "paging"
        },
        language: {
          search: "Suche:",
          searchPlaceholder: "Tabelle durchsuchen",
          lengthMenu: "_MENU_ Einträge pro Seite",
          info: "_START_ bis _END_ von _TOTAL_ Einträgen",
          infoEmpty: "Keine Einträge vorhanden",
          infoFiltered: "(gefiltert aus _MAX_ Einträgen)",
          zeroRecords: "Keine passenden Einträge gefunden",
          emptyTable: "Keine Daten vorhanden",
          paginate: {
            first: "Erste",
            previous: "Zurück",
            next: "Weiter",
            last: "Letzte"
          }
        },
        columnDefs: disabledColumnIndexes.length
          ? [
              {
                targets: disabledColumnIndexes,
                orderable: false,
                searchable: false
              }
            ]
          : []
      }
    );

    filterInputs.forEach(
      function (filter) {
        const column = dataTable.column(
          filter.columnIndex
        );

        filter.input.addEventListener(
          "input",
          function () {
            const searchValue =
              filter.input.value;

            if (
              column.search()
              !== searchValue
            ) {
              column
                .search(searchValue)
                .draw();
            }
          }
        );
      }
    );
  });
});